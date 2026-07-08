from asyncio import (
    FIRST_COMPLETED,
    Event,
    Task,
    create_task,
    gather,
    sleep as asyncio_sleep,
    wait,
)
from datetime import datetime, tzinfo
from html import escape
from json import JSONDecodeError, dumps as json_dumps
from logging import getLogger
from time import time
from typing import Any, Awaitable, Sequence

from httpx import AsyncClient, TransportError

from github_client import GithubError, Release, ReleaseGateway
from models import Item
from notify import Notifier, NotifyError
from palgate import PalgateClient, PalgateError
from service import GateWatcher
from state import StateStore

# Telegram long-poll window; the HTTP timeout must outlive it.
POLL_TIMEOUT = 25
ERROR_BACKOFF = 5
DEFAULT_LOG_COUNT = 5
MAX_LOG_COUNT = 20
MAX_VERSIONS = 10
RELEASE_NOTES_LIMIT = 1000

HELP_TEXT = (
    "<b>Commands</b>\n"
    "/status — service state\n"
    "/log [count] — last gate log entries (default %d, max %d)\n"
    "/poll — trigger an immediate poll cycle\n"
    "/pause — suspend polling (heartbeat stays alive)\n"
    "/resume — resume polling\n"
    "/release [version] — latest release info, or deploy a release\n"
    "/versions — list released versions\n"
    "/rollback [version] — redeploy a previous release\n"
    "/help — this message" % (DEFAULT_LOG_COUNT, MAX_LOG_COUNT)
)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)
    if not parts or secs:
        parts.append("%ds" % secs)
    return " ".join(parts[:2])


class OpsBot:
    """Serves operator commands from the ops chat via getUpdates polling.

    Only messages from the configured chat are honoured; everything else
    (other chats, plain text, commands addressed to another bot) is
    dropped silently. Like the polling loop, this loop never dies:
    transport failures back off and retry, and one broken update cannot
    take the others down. Replies go through ``replier`` — a Notifier
    bound to the same ops chat.
    """

    def __init__(
        self,
        http: AsyncClient,
        token: str,
        chat_id: int,
        watcher: GateWatcher,
        client: PalgateClient,
        store: StateStore,
        replier: Notifier,
        tz: tzinfo,
        version: str,
        github: ReleaseGateway | None = None,
    ) -> None:
        self._http = http
        self._base_url = "https://api.telegram.org/bot%s" % token
        self._chat_id = chat_id
        self._watcher = watcher
        self._client = client
        self._store = store
        self._replier = replier
        self._tz = tz
        self._version = version
        self._github = github
        self._offset = 0
        self._username: str | None = None
        self._log = getLogger("log")
        self._local = getLogger("default")

    async def run(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                if self._username is None:
                    await self._fetch_username()
                updates = await self._race(stop, self._fetch_updates())
            except TransportError as err:
                self._local.warning("Bot API request failed: %s" % err)
                await self._race(stop, self._backoff())
                continue
            except Exception:  # the loop must survive anything
                self._log.exception("Unexpected error in bot loop")
                await self._race(stop, self._backoff())
                continue
            for update in updates or ():
                try:
                    await self._handle(update)
                except Exception:
                    self._log.exception("Failed to handle bot update")

    async def _backoff(self) -> None:
        await asyncio_sleep(ERROR_BACKOFF)

    async def _race(self, stop: Event, awaitable: Awaitable[Any]) -> Any:
        """Run ``awaitable``, abandoning it as soon as ``stop`` is set.

        getUpdates blocks for up to POLL_TIMEOUT seconds; without the race
        a shutdown signal would have to wait out the whole long poll.
        """
        task: Task[Any] = create_task(_await(awaitable))
        stop_task = create_task(stop.wait())
        done, pending = await wait(
            (task, stop_task), return_when=FIRST_COMPLETED
        )
        for waiter in pending:
            waiter.cancel()
        await gather(*pending, return_exceptions=True)
        if task in done:
            return task.result()
        return None

    async def _fetch_username(self) -> None:
        response = await self._http.get(
            self._base_url + "/getMe", timeout=ERROR_BACKOFF * 2.0
        )
        payload = self._api_payload(response)
        if payload is None:
            return
        username = payload.get("username")
        if isinstance(username, str) and username:
            self._username = username
            self._local.info("Bot commands served as @%s" % username)

    async def _fetch_updates(self) -> list[dict[str, Any]]:
        response = await self._http.get(
            self._base_url + "/getUpdates",
            params={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": '["message"]',
            },
            timeout=POLL_TIMEOUT + 10.0,
        )
        payload = self._api_payload(response)
        if not isinstance(payload, list):
            return []
        updates = [item for item in payload if isinstance(item, dict)]
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Acknowledge before handling: a poison update must never
                # be re-fetched and wedge the command loop forever.
                self._offset = max(self._offset, update_id + 1)
        return updates

    def _api_payload(self, response: Any) -> Any:
        if response.status_code != 200:
            self._local.warning(
                "Bot API responded %d: %s"
                % (response.status_code, _truncate(response.text, 200))
            )
            return None
        try:
            body = response.json()
        except JSONDecodeError as err:
            self._local.warning("Bot API sent invalid JSON: %s" % err)
            return None
        if not isinstance(body, dict) or body.get("ok") is not True:
            self._local.warning(
                "Bot API refused the call: %s"
                % _truncate(json_dumps(body, default=str), 200)
            )
            return None
        return body.get("result")

    async def _handle(self, update: dict[str, Any]) -> None:
        command = self._parse_command(update)
        if command is None:
            return
        name, args = command
        self._local.info("Bot command /%s from ops chat" % name)
        reply = await self._dispatch(name, args)
        try:
            await self._replier.send(reply)
        except NotifyError as err:
            self._local.error("Cannot deliver bot reply: %s" % err)

    def _parse_command(
        self, update: dict[str, Any]
    ) -> tuple[str, list[str]] | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") != self._chat_id:
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return None
        parts = text.split()
        name, _, mention = parts[0][1:].partition("@")
        if mention and mention != self._username:
            return None
        if not name:
            return None
        return name.lower(), parts[1:]

    async def _dispatch(self, name: str, args: Sequence[str]) -> str:
        if name == "status":
            return await self._status_text()
        if name == "log":
            return await self._log_text(args)
        if name == "poll":
            self._watcher.poke()
            return "Poll cycle triggered."
        if name == "pause":
            if self._watcher.pause():
                return "Polling paused. Use /resume to continue."
            return "Polling is already paused."
        if name == "resume":
            if self._watcher.resume():
                return "Polling resumed."
            return "Polling is not paused."
        if name == "release":
            return await self._release_text(args)
        if name == "versions":
            return await self._versions_text()
        if name == "rollback":
            return await self._rollback_text(args)
        if name in ("help", "start"):
            return HELP_TEXT
        return "Unknown command /%s.\n\n%s" % (escape(name), HELP_TEXT)

    async def _rollback_text(self, args: Sequence[str]) -> str:
        if self._github is None:
            return "Rollback is not configured (set GITHUB_TOKEN)."
        try:
            tags = await self._github.release_tags()
        except GithubError as err:
            return "Cannot reach GitHub: %s" % escape(str(err))
        releases = escape(", ".join(tags)) if tags else "none"
        if not args:
            return (
                "Current version: %s\n"
                "Available releases: %s\n"
                "Usage: /rollback &lt;version&gt;"
                % (escape(self._version), releases)
            )
        target = args[0]
        if target == self._version:
            return "Already running %s. Nothing to do." % escape(target)
        if target not in tags:
            return "Unknown version %s. Available releases: %s" % (
                escape(target),
                releases,
            )
        try:
            await self._github.dispatch_deploy(target)
        except GithubError as err:
            return "Rollback dispatch failed: %s" % escape(str(err))
        return (
            "Rollback to %s triggered. The \"Rolled back … → %s\" notice "
            "here confirms success; /status shows the running version."
            % (escape(target), escape(target))
        )

    async def _release_text(self, args: Sequence[str]) -> str:
        if self._github is None:
            return "Release commands are not configured (set GITHUB_TOKEN)."
        try:
            releases = await self._github.releases()
        except GithubError as err:
            return "Cannot reach GitHub: %s" % escape(str(err))
        if not args:
            return self._release_screen(releases)
        target = args[0]
        if all(release.tag != target for release in releases):
            available = ", ".join(r.tag for r in releases) or "none"
            return "Unknown version %s. Available releases: %s" % (
                escape(target),
                escape(available),
            )
        try:
            await self._github.dispatch_deploy(target)
        except GithubError as err:
            return "Deploy dispatch failed: %s" % escape(str(err))
        if target == self._version:
            return (
                "Redeploy of the running version %s triggered. "
                "/status shows it once the swap completes." % escape(target)
            )
        return (
            "Deploy of %s triggered. The version change notice here "
            "confirms success; /status shows the running version."
            % escape(target)
        )

    def _release_screen(self, releases: Sequence[Release]) -> str:
        lines = []
        if not releases:
            lines.append("No releases yet.")
        else:
            latest = releases[0]
            header = "<b>Latest release: %s</b>" % escape(latest.tag)
            published = self._format_release_date(latest.published_at)
            if published is not None:
                header += " (%s)" % published
            lines.append(header)
            if latest.title is not None and latest.title != latest.tag:
                lines.append(escape(latest.title))
            if latest.notes is not None:
                lines.append(
                    escape(_truncate(latest.notes, RELEASE_NOTES_LIMIT))
                )
        lines.append("Running version: %s" % escape(self._version))
        lines.append("Usage: /release &lt;version&gt; — deploy that release")
        return "\n".join(lines)

    async def _versions_text(self) -> str:
        if self._github is None:
            return "Release commands are not configured (set GITHUB_TOKEN)."
        try:
            releases = await self._github.releases(MAX_VERSIONS)
        except GithubError as err:
            return "Cannot reach GitHub: %s" % escape(str(err))
        if not releases:
            return (
                "No releases yet. Running version: %s" % escape(self._version)
            )
        lines = ["<b>Releases</b> (newest first)"]
        for release in releases:
            line = escape(release.tag)
            published = self._format_release_date(release.published_at)
            if published is not None:
                line += " — %s" % published
            if release.tag == self._version:
                line += " (running)"
            lines.append(line)
        if all(release.tag != self._version for release in releases):
            lines.append("Running version: %s" % escape(self._version))
        return "\n".join(lines)

    def _format_release_date(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        return moment.astimezone(self._tz).strftime("%Y-%m-%d")

    async def _status_text(self) -> str:
        status = self._watcher.status()
        now = time()
        lines = ["<b>palgate-tg-notify %s</b>" % escape(self._version)]
        if status.started_at is not None:
            lines.append(
                "Uptime: %s" % format_duration(now - status.started_at)
            )
        state = "paused" if status.paused else "polling"
        lines.append("Source %s: %s" % (escape(status.source), state))
        lines.append("Consecutive failures: %d" % status.failures)
        lines.append("Last poll: %s" % self._format_time(status.last_poll_at))
        lines.append("Last success: %s" % self._format_time(status.last_ok_at))
        if status.next_poll_at is not None and not status.paused:
            lines.append(
                "Next poll: in %s"
                % format_duration(status.next_poll_at - now)
            )
        lines.append("Channels:")
        for channel in status.channels:
            marker = await self._store.get_marker(status.source, channel)
            lines.append(
                "  %s: %s"
                % (escape(channel), escape(marker or "not primed"))
            )
        return "\n".join(lines)

    async def _log_text(self, args: Sequence[str]) -> str:
        try:
            count = int(args[0]) if args else DEFAULT_LOG_COUNT
        except ValueError:
            return "Usage: /log [count] — count must be a number."
        count = max(1, min(MAX_LOG_COUNT, count))
        try:
            response = await self._client.fetch_log()
        except PalgateError as err:
            return "Cannot fetch the gate log: %s" % escape(str(err))
        items = (response.log or [])[:count]
        lines = ["<b>Last %d log entries</b> (newest first)" % len(items)]
        for item in items:
            timestamp = self._format_time(
                float(item.time) if item.time else None
            )
            lines.append("%s — %s" % (timestamp, Item.from_log_item(item)))
        return "\n".join(lines)

    def _format_time(self, timestamp: float | None) -> str:
        if timestamp is None:
            return "never"
        moment = datetime.fromtimestamp(timestamp, self._tz)
        return moment.strftime("%Y-%m-%d %H:%M:%S")


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable
