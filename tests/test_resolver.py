from pathlib import Path
from typing import Any, List

import pytest

from resolver import (
    CachingResolver,
    FileResolverStore,
    FloodError,
    Profile,
    ProfileCache,
    RateLimiter,
    ResolveOutcome,
)


class Clock:
    """Manually advanced clock injected as ``CachingResolver``'s time source."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class ScriptedRawResolver:
    """Raw ``PhoneResolver`` double replaying a script keyed by phone.

    A script value is a ``Profile`` / ``None`` (returned) or an ``Exception``
    (raised). Records every phone it was actually asked to resolve.
    """

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = dict(script)
        self.calls: List[str] = []

    async def resolve(
        self, phone: str, label: str | None = None
    ) -> Profile | None:
        self.calls.append(phone)
        result = self.script.get(phone)
        if isinstance(result, Exception):
            raise result
        assert result is None or isinstance(result, Profile)
        return result


PROFILE = Profile(user_id=42, username="neo", firstname="Thomas", lastname="A")


class TestProfile:
    def test_fullname_joins_present_parts(self) -> None:
        assert Profile(1, firstname="A", lastname="B").fullname == "A B"
        assert Profile(1, firstname="A").fullname == "A"
        assert Profile(1).fullname == ""

    def test_dict_roundtrip(self) -> None:
        assert Profile.from_dict(PROFILE.to_dict()) == PROFILE


class TestProfileCache:
    def test_miss_returns_none(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        assert cache.lookup("79001", now=0) is None

    def test_positive_hit_before_ttl(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        hit = cache.lookup("79001", now=99)
        assert hit is not None
        assert hit.outcome is ResolveOutcome.RESOLVED
        assert hit.profile == PROFILE

    def test_negative_hit_uses_short_ttl(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", None, now=0)
        hit = cache.lookup("79001", now=9)
        assert hit is not None
        assert hit.outcome is ResolveOutcome.ABSENT
        assert cache.lookup("79001", now=11) is None  # negative expired

    def test_expired_entry_is_dropped(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        assert cache.lookup("79001", now=101) is None

    def test_snapshot_restore_roundtrip(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        cache.put("79002", None, now=0)
        snap = cache.snapshot(now=0)

        restored = ProfileCache(positive_ttl=100, negative_ttl=10)
        restored.restore(snap, now=5)
        hit = restored.lookup("79001", now=5)
        assert hit is not None and hit.profile == PROFILE
        absent = restored.lookup("79002", now=5)
        assert absent is not None and absent.outcome is ResolveOutcome.ABSENT

    def test_restore_drops_already_expired(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        snap = cache.snapshot(now=0)

        restored = ProfileCache(positive_ttl=100, negative_ttl=10)
        restored.restore(snap, now=200)
        assert restored.lookup("79001", now=200) is None

    def test_snapshot_prunes_expired_entries(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        cache.put("79002", PROFILE, now=0)
        assert cache.snapshot(now=101) == {}  # both expired, pruned out

    def test_size_counts_only_live_entries(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        cache.put("79002", None, now=0)
        assert cache.size(now=0) == 2
        assert cache.size(now=50) == 1  # the negative entry expired

    def test_clear_drops_everything_and_reports_the_count(self) -> None:
        cache = ProfileCache(positive_ttl=100, negative_ttl=10)
        cache.put("79001", PROFILE, now=0)
        cache.put("79002", None, now=0)
        assert cache.clear() == 2
        assert cache.lookup("79001", now=0) is None
        assert cache.clear() == 0


class TestRateLimiter:
    def test_spacing_blocks_back_to_back(self) -> None:
        limiter = RateLimiter(min_interval=5, per_hour=100, per_day=100)
        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=4) is False
        assert limiter.try_acquire(now=5) is True

    def test_hourly_cap(self) -> None:
        limiter = RateLimiter(min_interval=0, per_hour=2, per_day=100)
        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=10) is True
        assert limiter.try_acquire(now=20) is False
        # once the first call ages out of the hour window, a slot frees up
        assert limiter.try_acquire(now=3601) is True

    def test_daily_cap(self) -> None:
        limiter = RateLimiter(min_interval=0, per_hour=100, per_day=2)
        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=3601) is True  # different hour
        assert limiter.try_acquire(now=7201) is False  # day cap hit

    def test_cooldown_blocks_until_deadline(self) -> None:
        limiter = RateLimiter(min_interval=0, per_hour=100, per_day=100)
        limiter.trigger_cooldown(seconds=100, now=0)  # +10% margin -> 110
        assert limiter.cooldown_remaining(now=0) == pytest.approx(110)
        assert limiter.try_acquire(now=100) is False
        assert limiter.try_acquire(now=111) is True

    def test_cooldown_only_extends(self) -> None:
        limiter = RateLimiter(min_interval=0, per_hour=100, per_day=100)
        limiter.trigger_cooldown(seconds=100, now=0)
        limiter.trigger_cooldown(seconds=1, now=0)  # shorter — must not shrink
        assert limiter.cooldown_remaining(now=0) == pytest.approx(110)

    def test_snapshot_restore_roundtrip(self) -> None:
        limiter = RateLimiter(min_interval=5, per_hour=100, per_day=100)
        limiter.try_acquire(now=0)
        limiter.trigger_cooldown(seconds=50, now=0)
        snap = limiter.snapshot(now=0)

        restored = RateLimiter(min_interval=5, per_hour=100, per_day=100)
        restored.restore(snap, now=1)
        assert restored.try_acquire(now=3) is False  # spacing from restored call
        assert restored.cooldown_remaining(now=1) == pytest.approx(54)


class TestCachingResolver:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_raw(self) -> None:
        raw = ScriptedRawResolver({"79001": PROFILE})
        resolver = _resolver(raw, Clock())
        first = await resolver.resolve("79001")
        second = await resolver.resolve("79001")
        assert first.outcome is ResolveOutcome.RESOLVED
        assert second.outcome is ResolveOutcome.RESOLVED
        assert raw.calls == ["79001"]  # second answered from cache

    @pytest.mark.asyncio
    async def test_absent_number_is_negatively_cached(self) -> None:
        raw = ScriptedRawResolver({"79001": None})
        resolver = _resolver(raw, Clock())
        result = await resolver.resolve("79001")
        assert result.outcome is ResolveOutcome.ABSENT
        assert result.known is True
        await resolver.resolve("79001")
        assert raw.calls == ["79001"]

    @pytest.mark.asyncio
    async def test_rate_limit_defers_without_calling_raw(self) -> None:
        raw = ScriptedRawResolver({"79001": PROFILE, "79002": PROFILE})
        limiter = RateLimiter(min_interval=5, per_hour=100, per_day=100)
        resolver = _resolver(raw, Clock(), limiter=limiter)
        await resolver.resolve("79001")
        deferred = await resolver.resolve("79002")  # inside spacing window
        assert deferred.outcome is ResolveOutcome.DEFERRED
        assert raw.calls == ["79001"]

    @pytest.mark.asyncio
    async def test_flood_error_triggers_cooldown(self) -> None:
        raw = ScriptedRawResolver({"79001": FloodError(30), "79002": PROFILE})
        clock = Clock()
        resolver = _resolver(raw, clock)
        deferred = await resolver.resolve("79001")
        assert deferred.outcome is ResolveOutcome.DEFERRED
        assert resolver.cooldown_remaining() == pytest.approx(33)  # 30 + 10%
        # a different number is now blocked by the cooldown, not attempted
        blocked = await resolver.resolve("79002")
        assert blocked.outcome is ResolveOutcome.DEFERRED
        assert raw.calls == ["79001"]

    @pytest.mark.asyncio
    async def test_generic_error_is_failed_not_cached(self) -> None:
        raw = ScriptedRawResolver({"79001": RuntimeError("boom")})
        resolver = _resolver(raw, Clock())
        result = await resolver.resolve("79001")
        assert result.outcome is ResolveOutcome.FAILED
        assert resolver.cached("79001") is None  # failure not cached

    @pytest.mark.asyncio
    async def test_cached_is_network_free(self) -> None:
        raw = ScriptedRawResolver({"79001": PROFILE})
        resolver = _resolver(raw, Clock())
        assert resolver.cached("79001") is None
        await resolver.resolve("79001")
        hit = resolver.cached("79001")
        assert hit is not None and hit.profile == PROFILE

    @pytest.mark.asyncio
    async def test_state_persists_across_instances(self, tmp_path: Path) -> None:
        store = FileResolverStore(tmp_path / "resolver.json")
        raw = ScriptedRawResolver({"79001": PROFILE})
        clock = Clock()
        first = _resolver(raw, clock, store=store)
        await first.resolve("79001")

        raw2 = ScriptedRawResolver({"79001": Profile(999)})  # would differ
        second = _resolver(raw2, clock, store=store)
        hit = second.cached("79001")
        assert hit is not None and hit.profile == PROFILE
        assert raw2.calls == []  # served from persisted cache

    @pytest.mark.asyncio
    async def test_cooldown_persists_across_restart(self, tmp_path: Path) -> None:
        store = FileResolverStore(tmp_path / "resolver.json")
        clock = Clock()
        raw = ScriptedRawResolver({"79001": FloodError(100)})
        first = _resolver(raw, clock, store=store)
        await first.resolve("79001")

        raw2 = ScriptedRawResolver({"79002": PROFILE})
        second = _resolver(raw2, clock, store=store)
        assert second.cooldown_remaining() == pytest.approx(110)
        blocked = await second.resolve("79002")
        assert blocked.outcome is ResolveOutcome.DEFERRED
        assert raw2.calls == []

    @pytest.mark.asyncio
    async def test_clear_cache_forces_a_fresh_lookup(self) -> None:
        raw = ScriptedRawResolver({"79001": PROFILE})
        resolver = _resolver(raw, Clock())
        await resolver.resolve("79001")
        assert resolver.cache_size() == 1

        assert resolver.clear_cache() == 1

        assert resolver.cache_size() == 0
        assert resolver.cached("79001") is None
        await resolver.resolve("79001")
        assert raw.calls == ["79001", "79001"]  # looked up again

    @pytest.mark.asyncio
    async def test_clear_cache_persists_and_keeps_the_limiter(
        self, tmp_path: Path
    ) -> None:
        store = FileResolverStore(tmp_path / "resolver.json")
        clock = Clock()
        raw = ScriptedRawResolver(
            {"79001": FloodError(100), "79002": PROFILE}
        )
        first = _resolver(raw, clock, store=store)
        await first.resolve("79002")
        await first.resolve("79001")  # trips the cooldown
        assert first.clear_cache() == 1

        second = _resolver(ScriptedRawResolver({}), clock, store=store)
        assert second.cache_size() == 0
        assert second.cached("79002") is None
        # the reset must not unlock the flood cooldown
        assert second.cooldown_remaining() == pytest.approx(110)


class _RaisingStore:
    """Store double that raises on load and/or save to test best-effort I/O."""

    def __init__(self, on_load: bool = False, on_save: bool = False) -> None:
        self._on_load = on_load
        self._on_save = on_save

    def load(self) -> dict[str, Any]:
        if self._on_load:
            raise OSError("load boom")
        return {}

    def save(self, document: dict[str, Any]) -> None:
        if self._on_save:
            raise OSError("save boom")


class TestResolverStateIsBestEffort:
    @pytest.mark.asyncio
    async def test_unreadable_state_does_not_crash_startup(self) -> None:
        raw = ScriptedRawResolver({"79001": PROFILE})
        resolver = _resolver(raw, Clock(), store=_RaisingStore(on_load=True))
        result = await resolver.resolve("79001")
        assert result.outcome is ResolveOutcome.RESOLVED

    @pytest.mark.asyncio
    async def test_unwritable_state_does_not_break_resolve(self) -> None:
        raw = ScriptedRawResolver({"79001": PROFILE})
        resolver = _resolver(raw, Clock(), store=_RaisingStore(on_save=True))
        result = await resolver.resolve("79001")
        assert result.outcome is ResolveOutcome.RESOLVED


class TestFileResolverStore:
    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        store = FileResolverStore(tmp_path / "resolver.json")
        assert store.load() == {}

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        store = FileResolverStore(tmp_path / "resolver.json")
        store.save({"cache": {"79001": 1}})
        assert store.load() == {"cache": {"79001": 1}}

    def test_corrupt_file_loads_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "resolver.json"
        path.write_text("{not json")
        assert FileResolverStore(path).load() == {}

    def test_non_dict_file_loads_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "resolver.json"
        path.write_text("[1, 2, 3]")
        assert FileResolverStore(path).load() == {}


def _resolver(
    raw: ScriptedRawResolver,
    clock: Clock,
    limiter: RateLimiter | None = None,
    store: FileResolverStore | None = None,
) -> CachingResolver:
    return CachingResolver(
        raw=raw,
        cache=ProfileCache(positive_ttl=1000, negative_ttl=100),
        limiter=limiter or RateLimiter(min_interval=0, per_hour=100, per_day=100),
        store=store,
        clock=clock,
    )
