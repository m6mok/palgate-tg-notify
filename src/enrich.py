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

    @property
    def resolver(self) -> CachingResolver:
        return self._resolver

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
        for phone, label in self._pending_lookups():
            result = await self._resolver.resolve(phone, label)
            if result.outcome is ResolveOutcome.DEFERRED:
                break  # rate limiter or cooldown blocked us — wait it out
        await self._flush_edits()

    def _pending_lookups(self) -> list[tuple[str, str | None]]:
        """Unique (phone, label) pairs still needing a lookup.

        ``label`` is the gate entry's name, passed through so the imported
        contact is saved under a meaningful name rather than a placeholder.
        """
        lookups: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for batch in self._queue:
            for item in batch.items:
                if not self._needs_lookup(item):
                    continue
                phone = _phone(item)
                if phone is not None and phone not in seen:
                    seen.add(phone)
                    lookups.append((phone, item.fullname or None))
        return lookups

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

    # Paper-plane glyph prefixed to the resolved link so it reads as a
    # Telegram reference at a glance.
    _TG_ICON = "✈️"

    @staticmethod
    def _suffix(profile: Profile) -> str:
        # Show the name the user set on their own Telegram profile (from the
        # resolve response), not the gate log's name. Fall back to the
        # @username, then a bare label. A public t.me link when there is a
        # username; otherwise the in-app tg:// profile link.
        label = profile.fullname or (
            "@" + profile.username if profile.username else "Telegram"
        )
        if profile.username:
            href = "https://t.me/%s" % profile.username
        else:
            href = "tg://user?id=%d" % profile.user_id
        return ' → <a href="%s">%s %s</a>' % (
            href,
            Enricher._TG_ICON,
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
