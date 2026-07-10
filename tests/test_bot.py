from asyncio import Event, create_task, sleep, wait_for
from datetime import timedelta, timezone
from typing import Any, Callable, Dict, List

import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Request, Response

import bot as bot_module
from bot import OpsBot, format_duration
from github_client import GithubError, Release
from notify import NotifyError
from palgate import TransientFetchError
from service import GateWatcher
from state import MemoryStateStore
from tests.conftest import (
    BASE_LOG_ITEM_DATA,
    SECOND_LOG_ITEM_DATA,
    RecordingNotifier,
    ScriptedPalgateClient,
    StubEnricher,
    make_response,
)

OPS_CHAT_ID = 987654321
BOT_USERNAME = "palgate_ops_bot"


def make_update(
    update_id: int, text: Any, chat_id: int = OPS_CHAT_ID
) -> Dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


class TelegramServerMock:
    """Scripted Bot API: answers getMe, replays getUpdates batches.

    A batch is either a list of updates (wrapped into an ok-response) or a
    raw ``Response``/exception for error-path tests. When the script runs
    dry, ``on_empty`` is called and an empty batch is returned.
    """

    def __init__(self, username: str | None = BOT_USERNAME) -> None:
        self.username = username
        self.batches: List[Any] = []
        self.requests: List[Request] = []
        self.on_empty: Callable[[], None] | None = None

    def handler(self, request: Request) -> Response:
        self.requests.append(request)
        if request.url.path.endswith("/getMe"):
            result: Dict[str, Any] = {"id": 1, "is_bot": True}
            if self.username is not None:
                result["username"] = self.username
            return Response(200, json={"ok": True, "result": result})
        if not self.batches:
            if self.on_empty is not None:
                self.on_empty()
            return Response(200, json={"ok": True, "result": []})
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        if isinstance(batch, Response):
            return batch
        return Response(200, json={"ok": True, "result": batch})

    def update_requests(self) -> List[Request]:
        return [
            request
            for request in self.requests
            if request.url.path.endswith("/getUpdates")
        ]


class FakeResolver:
    """CachingResolver test double: fixed cache size, records resets."""

    def __init__(self, size: int = 0, cooldown: float = 0.0) -> None:
        self._size = size
        self._cooldown = cooldown
        self.resets = 0

    def cache_size(self) -> int:
        return self._size

    def cooldown_remaining(self) -> float:
        return self._cooldown

    def clear_cache(self) -> int:
        self.resets += 1
        cleared, self._size = self._size, 0
        return cleared


def make_release(
    tag: str,
    title: str | None = None,
    published_at: str | None = None,
    notes: str | None = None,
) -> Release:
    return Release(
        tag=tag, title=title, published_at=published_at, notes=notes
    )


class ScriptedGithubClient:
    """ReleaseGateway test double: scripted releases, records dispatches."""

    def __init__(
        self,
        tags: List[str] | None = None,
        releases: List[Release] | None = None,
        list_error: GithubError | None = None,
        dispatch_error: GithubError | None = None,
    ) -> None:
        if releases is None:
            releases = [make_release(tag) for tag in (tags or [])]
        self.scripted = releases
        self.list_error = list_error
        self.dispatch_error = dispatch_error
        self.dispatched: List[str] = []
        self.prestable_dispatched: List[str] = []
        self.prestable_stops = 0
        self.promoted: List[str] = []

    async def releases(self, limit: int = 5) -> List[Release]:
        if self.list_error is not None:
            raise self.list_error
        return self.scripted[:limit]

    async def release_tags(self, limit: int = 5) -> List[str]:
        return [release.tag for release in await self.releases(limit)]

    async def dispatch_deploy(self, image_tag: str) -> None:
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.dispatched.append(image_tag)

    async def dispatch_prestable(self, image_tag: str) -> None:
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.prestable_dispatched.append(image_tag)

    async def dispatch_prestable_stop(self) -> None:
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.prestable_stops += 1

    async def dispatch_promote(self, image_tag: str) -> None:
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.promoted.append(image_tag)


def make_bot(
    batches: List[Any],
    watcher_script: List[Any] | None = None,
    client_script: List[Any] | None = None,
    store: MemoryStateStore | None = None,
    username: str | None = BOT_USERNAME,
    github: ScriptedGithubClient | None = None,
    mock_notifier: RecordingNotifier | None = None,
    enricher: StubEnricher | None = None,
    resolver: FakeResolver | None = None,
) -> tuple[OpsBot, GateWatcher, ScriptedPalgateClient, RecordingNotifier,
           TelegramServerMock, Event]:
    server = TelegramServerMock(username=username)
    server.batches = list(batches)
    stop = Event()
    server.on_empty = stop.set

    client = ScriptedPalgateClient(client_script or [])
    watcher_client = ScriptedPalgateClient(watcher_script or [])
    watcher = GateWatcher(
        source="gate",
        client=watcher_client,  # type: ignore[arg-type]
        store=store if store is not None else MemoryStateStore(),
        notifiers=(RecordingNotifier(name="telegram"),),
        cron_delay=0,
        enricher=enricher,
    )
    replier = RecordingNotifier(name="ops")
    ops_bot = OpsBot(
        http=AsyncClient(transport=MockTransport(server.handler)),
        token="test_token",
        chat_id=OPS_CHAT_ID,
        watcher=watcher,
        client=client,  # type: ignore[arg-type]
        store=watcher._store,
        replier=replier,
        tz=timezone(timedelta(hours=3)),
        version="1.2.3",
        github=github,
        mock_notifier=mock_notifier,
        resolver=resolver,  # type: ignore[arg-type]
    )
    return ops_bot, watcher, client, replier, server, stop


async def run_bot(ops_bot: OpsBot, stop: Event) -> None:
    await wait_for(ops_bot.run(stop), timeout=2)


class TestCommandFiltering:
    @pytest.mark.asyncio
    async def test_help_command_is_answered(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]]
        )

        await run_bot(ops_bot, stop)

        assert len(replier.sent) == 1
        assert "Commands" in replier.sent[0]
        assert "/status" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_messages_from_other_chats_are_ignored(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help", chat_id=111)]]
        )

        await run_bot(ops_bot, stop)

        assert replier.sent == []

    @pytest.mark.asyncio
    async def test_plain_text_and_broken_updates_are_ignored(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [
                [
                    make_update(1, "hello there"),
                    make_update(2, None),
                    {"update_id": 3},
                    {"update_id": 4, "message": {"chat": {"id": OPS_CHAT_ID}}},
                    make_update(5, "/"),
                ]
            ]
        )

        await run_bot(ops_bot, stop)

        assert replier.sent == []

    @pytest.mark.asyncio
    async def test_command_addressed_to_another_bot_is_ignored(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [
                [
                    make_update(1, "/help@other_bot"),
                    make_update(2, "/help@%s" % BOT_USERNAME),
                ]
            ]
        )

        await run_bot(ops_bot, stop)

        assert len(replier.sent) == 1

    @pytest.mark.asyncio
    async def test_unmentioned_command_works_without_username(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]], username=None
        )

        await run_bot(ops_bot, stop)

        assert len(replier.sent) == 1

    @pytest.mark.asyncio
    async def test_unknown_command_gets_the_help_text(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/frobnicate now")]]
        )

        await run_bot(ops_bot, stop)

        assert "Unknown command /frobnicate" in replier.sent[0]
        assert "Commands" in replier.sent[0]


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_status_reports_service_state_and_markers(self) -> None:
        store = MemoryStateStore()
        await store.advance("gate", "telegram", None, "1708675200:790012")
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/status")]], store=store
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "palgate-tg-notify 1.2.3" in reply
        assert "Source gate: polling" in reply
        assert "Last poll: never" in reply
        assert "telegram: 1708675200:790012" in reply

    @pytest.mark.asyncio
    async def test_status_shows_paused_state_and_unprimed_channel(
        self,
    ) -> None:
        ops_bot, watcher, _, replier, _, stop = make_bot(
            [[make_update(1, "/status")]]
        )
        watcher.pause()

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Source gate: paused" in reply
        assert "telegram: not primed" in reply


class TestLogCommand:
    @pytest.mark.asyncio
    async def test_log_lists_entries_newest_first(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/log")]],
            client_script=[
                make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA)
            ],
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Last 2 log entries" in reply
        assert reply.index("Jane Smith") < reply.index("John Doe")

    @pytest.mark.asyncio
    async def test_log_count_argument_limits_the_output(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/log 1")]],
            client_script=[
                make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA)
            ],
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Last 1 log entries" in reply
        assert "Jane Smith" in reply
        assert "John Doe" not in reply

    @pytest.mark.asyncio
    async def test_log_with_a_bad_count_explains_usage(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/log many")]]
        )

        await run_bot(ops_bot, stop)

        assert "Usage: /log" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_log_reports_fetch_failures(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/log")]],
            client_script=[TransientFetchError("palgate is down")],
        )

        await run_bot(ops_bot, stop)

        assert "Cannot fetch the gate log" in replier.sent[0]


class TestControlCommands:
    @pytest.mark.asyncio
    async def test_poll_pokes_the_watcher(self) -> None:
        ops_bot, watcher, _, replier, _, stop = make_bot(
            [[make_update(1, "/poll")]]
        )

        await run_bot(ops_bot, stop)

        assert "Poll cycle triggered" in replier.sent[0]
        assert watcher._poke_requested is True

    @pytest.mark.asyncio
    async def test_pause_and_resume_toggle_the_watcher(self) -> None:
        ops_bot, watcher, _, replier, _, stop = make_bot(
            [
                [
                    make_update(1, "/pause"),
                    make_update(2, "/pause"),
                    make_update(3, "/resume"),
                    make_update(4, "/resume"),
                ]
            ]
        )

        await run_bot(ops_bot, stop)

        assert "Polling paused" in replier.sent[0]
        assert "already paused" in replier.sent[1]
        assert "Polling resumed" in replier.sent[2]
        assert "not paused" in replier.sent[3]
        assert watcher.status().paused is False


class TestRollbackCommand:
    @pytest.mark.asyncio
    async def test_unconfigured_rollback_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback")]]
        )

        await run_bot(ops_bot, stop)

        assert "not configured" in replier.sent[0]
        assert "GITHUB_TOKEN" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_bare_rollback_lists_releases_and_usage(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3", "1.1.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback")]], github=github
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Current version: 1.2.3" in reply
        assert "2.0.0, 1.2.3, 1.1.0" in reply
        assert "Usage: /rollback" in reply
        assert github.dispatched == []

    @pytest.mark.asyncio
    async def test_valid_version_is_dispatched(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3", "1.1.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback 1.1.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.dispatched == ["1.1.0"]
        assert "Rollback to 1.1.0 triggered" in replier.sent[0]
        assert "Rolled back" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_current_version_is_refused(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback 1.2.3")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.dispatched == []
        assert "Already running 1.2.3" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_unknown_version_is_refused(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback 9.9.9")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.dispatched == []
        assert "Unknown version 9.9.9" in replier.sent[0]
        assert "2.0.0" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_github_outage_is_reported(self) -> None:
        github = ScriptedGithubClient(
            list_error=GithubError("GitHub responded 502")
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Cannot reach GitHub" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_failed_dispatch_is_reported(self) -> None:
        github = ScriptedGithubClient(
            tags=["1.1.0"],
            dispatch_error=GithubError("GitHub refused the dispatch: 422"),
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/rollback 1.1.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Rollback dispatch failed" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_help_mentions_rollback(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]]
        )

        await run_bot(ops_bot, stop)

        assert "/rollback" in replier.sent[0]


class TestReleaseCommand:
    @pytest.mark.asyncio
    async def test_unconfigured_release_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release")]]
        )

        await run_bot(ops_bot, stop)

        assert "not configured" in replier.sent[0]
        assert "GITHUB_TOKEN" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_bare_release_shows_the_latest_release_screen(self) -> None:
        github = ScriptedGithubClient(
            releases=[
                make_release(
                    "2.0.0",
                    title="Release pipeline",
                    published_at="2026-07-01T10:00:00Z",
                    notes="Adds the release pipeline & rollback.",
                ),
                make_release("1.2.3"),
            ]
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release")]], github=github
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Latest release: 2.0.0" in reply
        assert "2026-07-01" in reply
        assert "Release pipeline" in reply
        assert "Adds the release pipeline &amp; rollback." in reply
        assert "Running version: 1.2.3" in reply
        assert "Usage: /release &lt;version&gt;" in reply
        assert github.dispatched == []

    @pytest.mark.asyncio
    async def test_bare_release_without_releases_says_so(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release")]], github=ScriptedGithubClient()
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "No releases yet" in reply
        assert "Running version: 1.2.3" in reply

    @pytest.mark.asyncio
    async def test_valid_version_is_deployed(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release 2.0.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.dispatched == ["2.0.0"]
        assert "Deploy of 2.0.0 triggered" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_running_version_is_redeployed(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release 1.2.3")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.dispatched == ["1.2.3"]
        assert "Redeploy of the running version 1.2.3" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_unknown_version_is_refused(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release 9.9.9")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.dispatched == []
        assert "Unknown version 9.9.9" in replier.sent[0]
        assert "2.0.0" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_github_outage_is_reported(self) -> None:
        github = ScriptedGithubClient(
            list_error=GithubError("GitHub responded 502")
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Cannot reach GitHub" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_failed_dispatch_is_reported(self) -> None:
        github = ScriptedGithubClient(
            tags=["1.1.0"],
            dispatch_error=GithubError("GitHub refused the dispatch: 422"),
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/release 1.1.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Deploy dispatch failed" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_help_mentions_release_and_versions(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]]
        )

        await run_bot(ops_bot, stop)

        assert "/release" in replier.sent[0]
        assert "/versions" in replier.sent[0]


class TestVersionsCommand:
    @pytest.mark.asyncio
    async def test_unconfigured_versions_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/versions")]]
        )

        await run_bot(ops_bot, stop)

        assert "not configured" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_versions_lists_releases_and_marks_the_running_one(
        self,
    ) -> None:
        github = ScriptedGithubClient(
            releases=[
                make_release("2.0.0", published_at="2026-07-01T10:00:00Z"),
                make_release("1.2.3", published_at="2026-06-20T08:30:00Z"),
                make_release("1.1.0"),
            ]
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/versions")]], github=github
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Releases" in reply
        assert "2.0.0 — 2026-07-01" in reply
        assert "1.2.3 — 2026-06-20 (running)" in reply
        assert "1.1.0" in reply
        assert "Running version" not in reply

    @pytest.mark.asyncio
    async def test_unparseable_publish_date_is_omitted(self) -> None:
        github = ScriptedGithubClient(
            releases=[make_release("2.0.0", published_at="yesterday")]
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/versions")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "2.0.0\n" in replier.sent[0] + "\n"
        assert "yesterday" not in replier.sent[0]

    @pytest.mark.asyncio
    async def test_versions_names_the_running_version_when_unreleased(
        self,
    ) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.1.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/versions")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Running version: 1.2.3" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_versions_without_releases_says_so(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/versions")]], github=ScriptedGithubClient()
        )

        await run_bot(ops_bot, stop)

        assert "No releases yet" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_github_outage_is_reported(self) -> None:
        github = ScriptedGithubClient(
            list_error=GithubError("GitHub responded 502")
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/versions")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Cannot reach GitHub" in replier.sent[0]


class TestPrestableCommand:
    @pytest.mark.asyncio
    async def test_unconfigured_prestable_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable")]]
        )

        await run_bot(ops_bot, stop)

        assert "not configured" in replier.sent[0]
        assert "GITHUB_TOKEN" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_bare_prestable_lists_releases_and_usage(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable")]], github=github
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "2.0.0, 1.2.3" in reply
        assert "Usage: /prestable" in reply
        assert "/prestable stop" in reply
        assert github.prestable_dispatched == []

    @pytest.mark.asyncio
    async def test_valid_version_is_dispatched(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable 2.0.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.prestable_dispatched == ["2.0.0"]
        assert "Prestable deploy of 2.0.0 triggered" in replier.sent[0]
        assert "/promote 2.0.0" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_running_version_is_allowed(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable 1.2.3")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.prestable_dispatched == ["1.2.3"]

    @pytest.mark.asyncio
    async def test_unknown_version_is_refused(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable 9.9.9")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.prestable_dispatched == []
        assert "Unknown version 9.9.9" in replier.sent[0]
        assert "2.0.0" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_stop_is_dispatched(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable stop")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.prestable_stops == 1
        assert github.prestable_dispatched == []
        assert "Prestable stop triggered" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_github_outage_is_reported(self) -> None:
        github = ScriptedGithubClient(
            list_error=GithubError("GitHub responded 502")
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/prestable")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Cannot reach GitHub" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_failed_dispatch_is_reported(self) -> None:
        github = ScriptedGithubClient(
            tags=["1.1.0"],
            dispatch_error=GithubError("GitHub refused the dispatch: 422"),
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [
                [
                    make_update(1, "/prestable 1.1.0"),
                    make_update(2, "/prestable stop"),
                ]
            ],
            github=github,
        )

        await run_bot(ops_bot, stop)

        assert "Prestable dispatch failed" in replier.sent[0]
        assert "Prestable stop dispatch failed" in replier.sent[1]

    @pytest.mark.asyncio
    async def test_help_mentions_prestable_and_promote(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]]
        )

        await run_bot(ops_bot, stop)

        assert "/prestable" in replier.sent[0]
        assert "/promote" in replier.sent[0]


class TestMockCommand:
    @pytest.mark.asyncio
    async def test_unconfigured_mock_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/mock John Doe 79001234567")]]
        )

        await run_bot(ops_bot, stop)

        assert "not configured" in replier.sent[0]
        assert "PRESTABLE_TELEGRAM_CHAT_ID" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_mock_entry_is_posted_to_the_prestable_chat(self) -> None:
        mock_notifier = RecordingNotifier(name="prestable")
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/mock John Doe 79001234567")]],
            mock_notifier=mock_notifier,
        )

        await run_bot(ops_bot, stop)

        assert len(mock_notifier.sent) == 1
        message = mock_notifier.sent[0]
        assert "John Doe" in message
        assert '<a href="+79001234567">79001234567</a>' in message
        assert "📞" in message
        assert "Mock entry posted to the prestable chat" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_short_phone_is_normalized_like_a_real_entry(self) -> None:
        mock_notifier = RecordingNotifier(name="prestable")
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/mock Jane Smith 123456789")]],
            mock_notifier=mock_notifier,
        )

        await run_bot(ops_bot, stop)

        assert "79123456789" in mock_notifier.sent[0]

    @pytest.mark.asyncio
    async def test_html_in_arguments_is_escaped(self) -> None:
        mock_notifier = RecordingNotifier(name="prestable")
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/mock <b>John</b> Doe 79001234567")]],
            mock_notifier=mock_notifier,
        )

        await run_bot(ops_bot, stop)

        assert "<b>" not in mock_notifier.sent[0]
        assert "&lt;b&gt;John&lt;/b&gt;" in mock_notifier.sent[0]

    @pytest.mark.asyncio
    async def test_wrong_argument_count_explains_usage(self) -> None:
        mock_notifier = RecordingNotifier(name="prestable")
        ops_bot, _, _, replier, _, stop = make_bot(
            [
                [
                    make_update(1, "/mock"),
                    make_update(2, "/mock John Doe"),
                    make_update(3, "/mock John Doe 79001234567 extra"),
                ]
            ],
            mock_notifier=mock_notifier,
        )

        await run_bot(ops_bot, stop)

        assert mock_notifier.sent == []
        for reply in replier.sent:
            assert "Usage: /mock" in reply

    @pytest.mark.asyncio
    async def test_delivery_failure_is_reported(self) -> None:
        mock_notifier = RecordingNotifier(name="prestable")
        mock_notifier.fail_with = NotifyError("prestable chat is gone")
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/mock John Doe 79001234567")]],
            mock_notifier=mock_notifier,
        )

        await run_bot(ops_bot, stop)

        assert "Mock delivery failed" in replier.sent[0]
        assert "prestable chat is gone" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_mock_rides_the_enrichment_path(self) -> None:
        enricher = StubEnricher()
        mock_notifier = RecordingNotifier(name="prestable", message_id=42)
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/mock John Doe 79001234567")]],
            mock_notifier=mock_notifier,
            enricher=enricher,
        )

        await run_bot(ops_bot, stop)

        assert len(mock_notifier.sent) == 1
        assert mock_notifier.sent[0].startswith("ENRICHED:")
        assert len(enricher.tracked) == 1
        channel, message_id, items = enricher.tracked[0]
        assert (channel, message_id) == ("prestable", 42)
        assert len(items) == 1
        assert "Mock entry posted to the prestable chat" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_help_mentions_mock(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]]
        )

        await run_bot(ops_bot, stop)

        assert "/mock" in replier.sent[0]


class TestResolveCommand:
    @pytest.mark.asyncio
    async def test_without_a_resolver_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/resolve")]]
        )

        await run_bot(ops_bot, stop)

        assert "not running" in replier.sent[0]
        assert "RESOLVE_ENABLED" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_bare_resolve_shows_cache_state_and_usage(self) -> None:
        resolver = FakeResolver(size=7)
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/resolve")]], resolver=resolver
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Cached numbers: 7" in reply
        assert "cooldown" not in reply
        assert "Usage: /resolve reset" in reply
        assert resolver.resets == 0

    @pytest.mark.asyncio
    async def test_bare_resolve_shows_an_active_cooldown(self) -> None:
        resolver = FakeResolver(size=1, cooldown=125.0)
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/resolve")]], resolver=resolver
        )

        await run_bot(ops_bot, stop)

        assert "Flood cooldown: 2m 5s left" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_reset_clears_the_cache_and_reports_the_count(self) -> None:
        resolver = FakeResolver(size=7)
        ops_bot, _, _, replier, _, stop = make_bot(
            [
                [
                    make_update(1, "/resolve reset"),
                    make_update(2, "/resolve"),
                ]
            ],
            resolver=resolver,
        )

        await run_bot(ops_bot, stop)

        assert resolver.resets == 1
        assert "7 number(s) dropped" in replier.sent[0]
        assert "Cached numbers: 0" in replier.sent[1]

    @pytest.mark.asyncio
    async def test_help_mentions_resolve(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help")]]
        )

        await run_bot(ops_bot, stop)

        assert "/resolve" in replier.sent[0]


class TestPromoteCommand:
    @pytest.mark.asyncio
    async def test_unconfigured_promote_is_refused(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/promote")]]
        )

        await run_bot(ops_bot, stop)

        assert "not configured" in replier.sent[0]
        assert "GITHUB_TOKEN" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_bare_promote_lists_releases_and_usage(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/promote")]], github=github
        )

        await run_bot(ops_bot, stop)

        reply = replier.sent[0]
        assert "Current version: 1.2.3" in reply
        assert "2.0.0, 1.2.3" in reply
        assert "Usage: /promote" in reply
        assert github.promoted == []

    @pytest.mark.asyncio
    async def test_valid_version_is_dispatched(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0", "1.2.3"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/promote 2.0.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.promoted == ["2.0.0"]
        assert "Promote of 2.0.0 triggered" in replier.sent[0]
        assert "prestable stops" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_unknown_version_is_refused(self) -> None:
        github = ScriptedGithubClient(tags=["2.0.0"])
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/promote 9.9.9")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert github.promoted == []
        assert "Unknown version 9.9.9" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_github_outage_is_reported(self) -> None:
        github = ScriptedGithubClient(
            list_error=GithubError("GitHub responded 502")
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/promote")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Cannot reach GitHub" in replier.sent[0]

    @pytest.mark.asyncio
    async def test_failed_dispatch_is_reported(self) -> None:
        github = ScriptedGithubClient(
            tags=["1.1.0"],
            dispatch_error=GithubError("GitHub refused the dispatch: 422"),
        )
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/promote 1.1.0")]], github=github
        )

        await run_bot(ops_bot, stop)

        assert "Promote dispatch failed" in replier.sent[0]


class TestLoopResilience:
    @pytest.mark.asyncio
    async def test_offset_acknowledges_processed_updates(self) -> None:
        ops_bot, _, _, _, server, stop = make_bot(
            [[make_update(7, "/help")]]
        )

        await run_bot(ops_bot, stop)

        first, second = server.update_requests()[:2]
        assert "offset=0" in str(first.url)
        assert "offset=8" in str(second.url)

    @pytest.mark.asyncio
    async def test_loop_survives_api_and_transport_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bot_module, "ERROR_BACKOFF", 0.01)
        ops_bot, _, _, replier, _, stop = make_bot(
            [
                Response(500, text="gateway exploded"),
                Response(200, content=b"not json"),
                Response(200, json={"ok": False, "error_code": 401}),
                ConnectError("no route to telegram"),
                [make_update(1, "/help")],
            ]
        )

        await run_bot(ops_bot, stop)

        assert len(replier.sent) == 1

    @pytest.mark.asyncio
    async def test_loop_survives_reply_delivery_failures(self) -> None:
        ops_bot, _, _, replier, _, stop = make_bot(
            [[make_update(1, "/help"), make_update(2, "/poll")]]
        )
        replier.fail_with = NotifyError("ops chat is gone")

        await run_bot(ops_bot, stop)

        assert replier.sent == []

    @pytest.mark.asyncio
    async def test_stop_interrupts_a_pending_long_poll(self) -> None:
        ops_bot, _, _, _, server, stop = make_bot([])

        async def slow_handler(request: Request) -> Response:
            await sleep(10)
            return Response(200, json={"ok": True, "result": []})

        server.on_empty = None
        ops_bot._http = AsyncClient(transport=MockTransport(slow_handler))
        ops_bot._username = BOT_USERNAME  # skip getMe

        task = create_task(ops_bot.run(stop))
        await sleep(0.05)
        stop.set()

        await wait_for(task, timeout=1)


class TestFormatDuration:
    def test_seconds_only(self) -> None:
        assert format_duration(42) == "42s"

    def test_minutes_and_seconds(self) -> None:
        assert format_duration(125) == "2m 5s"

    def test_days_and_hours_drop_the_tail(self) -> None:
        assert format_duration(90061) == "1d 1h"

    def test_negative_is_clamped_to_zero(self) -> None:
        assert format_duration(-5) == "0s"
