"""Best-effort message enrichment: append resolved Telegram identities.

A delivered notification lists gate entries by phone number. The enricher
looks each number up (via the anti-flood ``CachingResolver``) and edits the
message to append the matching Telegram identity.

Two paths, both best-effort — enrichment never blocks or fails delivery:

* **immediate** — ``render`` folds in whatever is already in the resolver
  cache when the message is first built, so warm numbers arrive enriched with
  no edit at all;
* **background dogon** — ``track`` queues the numbers that still need a
  network lookup; ``run`` drains that queue at the rate limiter's pace and
  re-edits each message as its numbers resolve.

The queue is in-memory: a restart drops pending dogon (those messages stay at
their last edited state), but the resolver cache is persisted, so future
messages still benefit. A batch is dropped once every number is known, once
an edit is permanently rejected, or once it outlives ``batch_ttl``.
"""

from asyncio import FIRST_COMPLETED, Event, create_task, gather, wait
from dataclasses import dataclass
from html import escape
from logging import getLogger
from time import time
from typing import Callable, Sequence

from models import Item
from notify import Notifier, NotifyError
from resolver import CachingResolver, Profile, ResolveOutcome


def _phone(item: Item) -> str | None:
    try:
        return item.pn
    except ValueError:
        return None


@dataclass
class _Batch:
    notifier: Notifier
    message_id: int
    items: tuple[Item, ...]
    last_text: str
    created_at: float


class Enricher:
    def __init__(
        self,
        resolver: CachingResolver,
        poll_interval: float = 5.0,
        batch_ttl: float = 3600.0,
        clock: Callable[[], float] = time,
    ) -> None:
        self._resolver = resolver
        self._poll_interval = poll_interval
        self._batch_ttl = batch_ttl
        self._clock = clock
        self._queue: list[_Batch] = []
        self._wake = Event()
        self._log = getLogger("default")

    def render(self, items: Sequence[Item]) -> str:
        """The batch text with any cached Telegram identities appended."""
        return "\n".join(self._line(item) for item in items)

    def track(
        self, notifier: Notifier, message_id: int, items: Sequence[Item]
    ) -> None:
        """Queue a delivered batch for background dogon if anything is unknown.

        Called after a successful send. Numbers already resolved from cache
        were folded in by ``render`` at send time and need no follow-up.
        """
        if not any(self._needs_lookup(item) for item in items):
            return
        self._queue.append(
            _Batch(
                notifier=notifier,
                message_id=message_id,
                items=tuple(items),
                last_text=self.render(items),
                created_at=self._clock(),
            )
        )
        self._wake.set()

    async def run(self, stop: Event) -> None:
        """Drain the dogon queue until stopped; never raises."""
        while not stop.is_set():
            try:
                await self._drain_once()
            except Exception:  # a worker crash must not take the loop down
                self._log.exception("Enricher round failed")
            await self._sleep(stop)

    async def _drain_once(self) -> None:
        self._expire_stale()
        if not self._queue:
            return
        for phone in self._pending_phones():
            result = await self._resolver.resolve(phone)
            if result.outcome is ResolveOutcome.DEFERRED:
                break  # rate limiter or cooldown blocked us — wait it out
        await self._flush_edits()

    def _pending_phones(self) -> list[str]:
        phones: list[str] = []
        seen: set[str] = set()
        for batch in self._queue:
            for item in batch.items:
                if not self._needs_lookup(item):
                    continue
                phone = _phone(item)
                if phone is not None and phone not in seen:
                    seen.add(phone)
                    phones.append(phone)
        return phones

    async def _flush_edits(self) -> None:
        for batch in list(self._queue):
            text = self.render(batch.items)
            if text != batch.last_text:
                try:
                    await batch.notifier.edit(batch.message_id, text)
                except NotifyError as err:
                    if err.permanent:
                        self._log.error(
                            "Enrich edit permanently rejected, dropping: %s" % err
                        )
                        self._queue.remove(batch)
                        continue
                    self._log.warning("Enrich edit failed, will retry: %s" % err)
                    continue  # keep last_text so we retry the same edit
                batch.last_text = text
            if self._is_complete(batch):
                self._queue.remove(batch)

    def _expire_stale(self) -> None:
        deadline = self._clock() - self._batch_ttl
        self._queue = [b for b in self._queue if b.created_at > deadline]

    def _is_complete(self, batch: _Batch) -> bool:
        return not any(self._needs_lookup(item) for item in batch.items)

    def _needs_lookup(self, item: Item) -> bool:
        phone = _phone(item)
        if phone is None:
            return False
        return self._resolver.cached(phone) is None

    def _line(self, item: Item) -> str:
        base = str(item)
        phone = _phone(item)
        if phone is None:
            return base
        hit = self._resolver.cached(phone)
        if (
            hit is None
            or hit.outcome is not ResolveOutcome.RESOLVED
            or hit.profile is None
        ):
            return base
        return base + self._suffix(hit.profile)

    @staticmethod
    def _suffix(profile: Profile) -> str:
        label = (
            "@" + profile.username
            if profile.username
            else (profile.fullname or "Telegram")
        )
        return ' → <a href="tg://user?id=%d">%s</a>' % (
            profile.user_id,
            escape(label),
        )

    async def _sleep(self, stop: Event) -> None:
        if stop.is_set():
            return
        self._wake.clear()
        waiters = (create_task(stop.wait()), create_task(self._wake.wait()))
        _, pending = await wait(
            waiters, timeout=self._poll_interval, return_when=FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await gather(*pending, return_exceptions=True)
