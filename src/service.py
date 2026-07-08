from asyncio import Event, wait_for
from itertools import takewhile
from logging import getLogger
from pathlib import Path
from random import uniform
from time import time
from typing import Sequence

from models import Item, LogItem
from notify import Notifier, NotifyError
from palgate import PalgateClient, PalgateError
from state import StateStore

# How far past the next planned poll the heartbeat stays valid; covers a
# slow poll cycle (retries inside the client) plus scheduling slack.
HEARTBEAT_MARGIN = 60


def item_key(item: LogItem) -> str:
    """Stable dedup key: equality of full models breaks as soon as the API
    mutates any field of an already-seen entry."""
    return "%s:%s" % (item.time, item.sn or item.userId or "")


class GateWatcher:
    """Polls one gate and fans deliveries out to notification channels.

    Reliability contract: at-least-once. The per-channel marker advances
    only after the channel confirmed delivery, so a failed send is retried
    on the next cycle; a duplicate is preferred over a lost notification.
    The polling loop never dies — failures escalate through exponential
    backoff and an alert to the ops log after ``alert_after`` bad cycles.
    """

    def __init__(
        self,
        source: str,
        client: PalgateClient,
        store: StateStore,
        notifiers: Sequence[Notifier],
        cron_delay: float,
        max_backoff: float = 300,
        alert_after: int = 10,
        heartbeat_path: Path | None = None,
    ) -> None:
        self._source = source
        self._client = client
        self._store = store
        self._notifiers = tuple(notifiers)
        self._cron_delay = cron_delay
        self._max_backoff = max_backoff
        self._alert_after = alert_after
        self._heartbeat_path = heartbeat_path
        self._heartbeat_ok = True
        self._log = getLogger("log")
        self._local = getLogger("default")

    async def run(self, stop: Event) -> None:
        failures = 0
        while not stop.is_set():
            ok = False
            error = "unknown"
            try:
                ok = await self.poll_once()
                error = "delivery failed"
            except PalgateError as err:
                self._local.error("Poll failed: %s" % err)
                error = str(err)
            except Exception as err:  # the loop must survive anything
                self._log.exception("Unexpected error in poll cycle")
                error = repr(err)

            if ok:
                if failures >= self._alert_after:
                    self._log.info(
                        "Recovered after %d failed cycles" % failures
                    )
                failures = 0
                delay = self._cron_delay
            else:
                failures += 1
                delay = self._backoff(failures)
                if failures % self._alert_after == 0:
                    self._log.error(
                        "Source %s is failing for %d cycles, last error: %s"
                        % (self._source, failures, error)
                    )

            self._touch_heartbeat(delay)
            try:
                await wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def poll_once(self) -> bool:
        """One fetch + fan-out cycle; True when every channel is caught up."""
        response = await self._client.fetch_log()
        items = response.log or []  # non-empty, enforced by ItemResponse
        ok = True
        for notifier in self._notifiers:
            ok = await self._deliver(notifier, items) and ok
        return ok

    async def _deliver(
        self, notifier: Notifier, items: Sequence[LogItem]
    ) -> bool:
        head_key = item_key(items[0])
        marker = await self._store.get_marker(self._source, notifier.name)
        if marker is None:
            # First poll for this channel: prime the marker silently
            # instead of replaying the whole visible history.
            await self._store.advance(
                self._source, notifier.name, None, head_key
            )
            self._local.debug(
                "Primed %s/%s marker at %s"
                % (self._source, notifier.name, head_key)
            )
            return True

        new_items = tuple(
            takewhile(lambda item: item_key(item) != marker, items)
        )
        if not new_items:
            return True

        message = "\n".join(
            str(Item.from_log_item(item)) for item in reversed(new_items)
        )
        try:
            await notifier.send(message)
        except NotifyError as err:
            if not err.permanent:
                self._log.error(
                    "Delivery to %s failed, will retry: %s"
                    % (notifier.name, err)
                )
                return False
            # The channel will never accept this message — advancing the
            # marker anyway keeps one poison batch from blocking the
            # channel forever.
            self._log.error(
                "%s permanently rejected the batch, skipping it: %s"
                % (notifier.name, err)
            )
        else:
            self._local.info("Delivered to %s:\n%s" % (notifier.name, message))

        if not await self._store.advance(
            self._source, notifier.name, marker, head_key
        ):
            self._log.error(
                "Marker %s/%s moved concurrently, batch may repeat"
                % (self._source, notifier.name)
            )
        return True

    def _backoff(self, failures: int) -> float:
        base = float(max(self._cron_delay, 1))
        capped = min(self._max_backoff, base * float(2 ** (failures - 1)))
        return capped * uniform(1.0, 1.25)

    def _touch_heartbeat(self, next_delay: float) -> None:
        if self._heartbeat_path is None:
            return
        deadline = time() + next_delay + HEARTBEAT_MARGIN
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.write_text("%f" % deadline)
        except OSError as err:
            # A broken heartbeat only degrades the healthcheck signal; it
            # must not take the polling loop down with it. The first
            # failure is escalated to the ops chat (the container will
            # look unhealthy while the loop is actually alive); repeats
            # stay local to avoid spamming every cycle.
            if self._heartbeat_ok:
                self._log.error("Cannot write heartbeat: %s" % err)
                self._heartbeat_ok = False
            else:
                self._local.error("Cannot write heartbeat: %s" % err)
        else:
            if not self._heartbeat_ok:
                self._log.info("Heartbeat restored")
            self._heartbeat_ok = True
