from asyncio import Event, wait_for
from typing import Any, List

import pytest

from tests.conftest import BASE_LOG_ITEM_DATA, RecordingNotifier
from enrich import Enricher
from models import Item
from notify import NotifyError
from resolver import (
    CachingResolver,
    FloodError,
    Profile,
    ProfileCache,
    RateLimiter,
)


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class ScriptedRawResolver:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = dict(script)
        self.calls: List[str] = []

    async def resolve(self, phone: str) -> Profile | None:
        self.calls.append(phone)
        result = self.script.get(phone)
        if isinstance(result, Exception):
            raise result
        assert result is None or isinstance(result, Profile)
        return result


def make_item(phone: str, first: str = "John", last: str = "Doe") -> Item:
    data = dict(BASE_LOG_ITEM_DATA)
    data.update({"sn": phone, "userId": "0", "firstname": first, "lastname": last})
    return Item(**data)


NEO = Profile(user_id=42, username="neo", firstname="Thomas", lastname="Anderson")


def build(
    script: dict[str, Any],
    clock: Clock,
    limiter: RateLimiter | None = None,
) -> tuple[Enricher, ScriptedRawResolver, CachingResolver]:
    raw = ScriptedRawResolver(script)
    resolver = CachingResolver(
        raw=raw,
        cache=ProfileCache(positive_ttl=1000, negative_ttl=100),
        limiter=limiter
        or RateLimiter(min_interval=0, per_hour=100, per_day=100),
        clock=clock,
    )
    enricher = Enricher(resolver, poll_interval=0.01, clock=clock)
    return enricher, raw, resolver


class TestRender:
    def test_phone_fallback_link_on_cache_miss(self) -> None:
        enricher, _, _ = build({"79001234567": NEO}, Clock())
        item = make_item("79001234567")
        rendered = enricher.render([item])
        assert rendered.startswith(str(item))
        assert rendered.endswith(
            ' → <a href="https://t.me/+79001234567">✈️ Telegram</a>'
        )

    @pytest.mark.asyncio
    async def test_appends_telegram_name_linked_to_tme(self) -> None:
        enricher, _, resolver = build({"79001234567": NEO}, Clock())
        await resolver.resolve("79001234567")  # warm the cache
        item = make_item("79001234567")
        rendered = enricher.render([item])
        assert rendered.startswith(str(item))
        # the user's own Telegram name, linked to t.me/<username>
        assert rendered.endswith(
            ' → <a href="https://t.me/neo">✈️ Thomas Anderson</a>'
        )

    @pytest.mark.asyncio
    async def test_username_shown_when_no_telegram_name(self) -> None:
        profile = Profile(user_id=5, username="solo")
        enricher, _, resolver = build({"79001234567": profile}, Clock())
        await resolver.resolve("79001234567")
        rendered = enricher.render([make_item("79001234567")])
        assert rendered.endswith(
            ' → <a href="https://t.me/solo">✈️ @solo</a>'
        )

    @pytest.mark.asyncio
    async def test_absent_number_gets_phone_fallback_link(self) -> None:
        enricher, _, resolver = build({"79001234567": None}, Clock())
        await resolver.resolve("79001234567")
        item = make_item("79001234567")
        rendered = enricher.render([item])
        assert rendered.startswith(str(item))
        assert rendered.endswith(
            ' → <a href="https://t.me/+79001234567">✈️ Telegram</a>'
        )

    @pytest.mark.asyncio
    async def test_telegram_name_is_escaped_and_phone_link_without_username(
        self,
    ) -> None:
        profile = Profile(user_id=7, username=None, firstname="A<b>", lastname="X")
        enricher, _, resolver = build({"79001234567": profile}, Clock())
        await resolver.resolve("79001234567")
        rendered = enricher.render([make_item("79001234567")])
        # not tg://user?id — the Bot API strips that entity for users the
        # bot has never seen, leaving bare unlinked text
        assert rendered.endswith(
            ' → <a href="https://t.me/+79001234567">✈️ A&lt;b&gt; X</a>'
        )


class TestTrack:
    def test_no_enqueue_when_all_known(self) -> None:
        enricher, _, _ = build({}, Clock())
        # a number with no phone-resolvable data is treated as "nothing to do"
        # here we simply assert an empty batch queues nothing
        enricher.track(RecordingNotifier(), 1, [])
        assert enricher._queue == []

    def test_enqueue_on_unknown(self) -> None:
        enricher, _, _ = build({"79001234567": NEO}, Clock())
        enricher.track(RecordingNotifier(message_id=1), 1, [make_item("79001234567")])
        assert len(enricher._queue) == 1


class TestDogon:
    @pytest.mark.asyncio
    async def test_resolves_and_edits_message(self) -> None:
        enricher, raw, _ = build({"79001234567": NEO}, Clock())
        notifier = RecordingNotifier(message_id=555)
        item = make_item("79001234567")
        enricher.track(notifier, 555, [item])

        await enricher._drain_once()

        assert raw.calls == ["79001234567"]
        assert len(notifier.edited) == 1
        message_id, text = notifier.edited[0]
        assert message_id == 555
        assert text.endswith(
            ' → <a href="https://t.me/neo">✈️ Thomas Anderson</a>'
        )
        assert enricher._queue == []  # completed and dropped

    @pytest.mark.asyncio
    async def test_rename_is_picked_up_for_a_cached_number(self) -> None:
        enricher, raw, resolver = build({"79001234567": NEO}, Clock())
        await resolver.resolve("79001234567")  # warm cache: old name
        raw.script["79001234567"] = Profile(
            user_id=42, username="neo", firstname="Mr", lastname="Smith"
        )
        notifier = RecordingNotifier(message_id=1)
        enricher.track(notifier, 1, [make_item("79001234567")])

        await enricher._drain_once()

        # sent with the cached name, then re-checked and edited to the new one
        assert raw.calls == ["79001234567", "79001234567"]
        assert notifier.edited[-1][1].endswith(
            ' → <a href="https://t.me/neo">✈️ Mr Smith</a>'
        )
        assert enricher._queue == []

    @pytest.mark.asyncio
    async def test_cached_absent_number_is_not_rechecked(self) -> None:
        enricher, raw, resolver = build({"79001234567": None}, Clock())
        await resolver.resolve("79001234567")  # negative-cached
        enricher.track(
            RecordingNotifier(message_id=1), 1, [make_item("79001234567")]
        )
        assert enricher._queue == []
        assert raw.calls == ["79001234567"]  # only the warm-up call

    @pytest.mark.asyncio
    async def test_absent_batch_completes_without_edit(self) -> None:
        enricher, _, _ = build({"79001234567": None}, Clock())
        notifier = RecordingNotifier(message_id=1)
        enricher.track(notifier, 1, [make_item("79001234567")])

        await enricher._drain_once()

        assert notifier.edited == []  # nothing changed, no edit
        assert enricher._queue == []  # still dropped as complete

    @pytest.mark.asyncio
    async def test_rate_limit_defers_and_keeps_batch(self) -> None:
        limiter = RateLimiter(min_interval=100, per_hour=100, per_day=100)
        enricher, raw, resolver = build(
            {"79001234567": NEO, "79009876543": NEO}, Clock(), limiter=limiter
        )
        notifier = RecordingNotifier(message_id=1)
        items = [make_item("79001234567"), make_item("79009876543")]
        enricher.track(notifier, 1, items)

        await enricher._drain_once()

        # first number consumed the only slot; second deferred, batch stays
        assert raw.calls == ["79001234567"]
        assert len(enricher._queue) == 1

    @pytest.mark.asyncio
    async def test_flood_error_pauses_dogon(self) -> None:
        enricher, raw, resolver = build({"79001234567": FloodError(50)}, Clock())
        notifier = RecordingNotifier(message_id=1)
        enricher.track(notifier, 1, [make_item("79001234567")])

        await enricher._drain_once()

        assert resolver.cooldown_remaining() == pytest.approx(55)
        assert notifier.edited == []
        assert len(enricher._queue) == 1  # retried later

    @pytest.mark.asyncio
    async def test_permanent_edit_error_drops_batch(self) -> None:
        enricher, _, _ = build({"79001234567": NEO}, Clock())
        notifier = RecordingNotifier(message_id=1)
        notifier.fail_with = NotifyError("gone", permanent=True)
        # fail_with only affects send(); make edit raise instead:
        notifier.edit = _raise_permanent  # type: ignore[method-assign]
        enricher.track(notifier, 1, [make_item("79001234567")])

        await enricher._drain_once()

        assert enricher._queue == []

    @pytest.mark.asyncio
    async def test_transient_edit_error_keeps_batch(self) -> None:
        enricher, _, _ = build({"79001234567": NEO}, Clock())
        notifier = RecordingNotifier(message_id=1)
        notifier.edit = _raise_transient  # type: ignore[method-assign]
        enricher.track(notifier, 1, [make_item("79001234567")])

        await enricher._drain_once()

        assert len(enricher._queue) == 1

    @pytest.mark.asyncio
    async def test_stale_batch_expires(self) -> None:
        clock = Clock()
        enricher, _, _ = build({"79001234567": NEO}, clock)
        enricher = Enricher(
            enricher._resolver, poll_interval=0.01, batch_ttl=10, clock=clock
        )
        enricher.track(RecordingNotifier(message_id=1), 1, [make_item("79001234567")])
        clock.tick(20)

        await enricher._drain_once()

        assert enricher._queue == []

    @pytest.mark.asyncio
    async def test_run_loop_stops_on_event(self) -> None:
        enricher, _, _ = build({"79001234567": NEO}, Clock())
        notifier = RecordingNotifier(message_id=1)
        enricher.track(notifier, 1, [make_item("79001234567")])
        stop = Event()

        async def runner() -> None:
            await enricher.run(stop)

        import asyncio

        task = asyncio.create_task(runner())
        # let it drain at least once, then stop
        while not notifier.edited:
            await asyncio.sleep(0.005)
        stop.set()
        await wait_for(task, timeout=1)
        assert notifier.edited


async def _raise_permanent(message_id: int, text: str) -> None:
    raise NotifyError("nope", permanent=True)


async def _raise_transient(message_id: int, text: str) -> None:
    raise NotifyError("later", permanent=False)
