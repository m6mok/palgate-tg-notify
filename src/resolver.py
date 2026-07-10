"""Phone-number → Telegram profile resolution with an anti-flood layer.

Resolving a phone number to a Telegram identity is only possible through a
user account (MTProto ``contacts.importContacts``), and Telegram rate-limits
that call aggressively. This module keeps the raw lookup (``PhoneResolver``)
behind three cheap guards, from cheapest to most expensive:

1. a TTL cache (``ProfileCache``) — the same people walk through the gate
   every day, so a warm cache means almost no network calls;
2. a token-bucket ``RateLimiter`` — spacing plus hourly/daily caps;
3. a persisted FloodWait cooldown — a ``FloodError`` disables lookups for the
   window Telegram asks for, and the deadline survives restarts so a reboot
   cannot walk straight back into the same flood.

``CachingResolver`` composes them. Every lookup is best-effort: when a guard
blocks the call the resolver returns ``DEFERRED``/``FAILED`` instead of
raising, so the caller can retry later without ever affecting notification
delivery.
"""

from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError, dump as json_dump, load as json_load
from logging import getLogger
from os import fsync, replace
from pathlib import Path
from time import time
from typing import Any, Callable, Protocol

HOUR = 3600.0
DAY = 86400.0


@dataclass(frozen=True)
class Profile:
    """A resolved Telegram identity for a phone number."""

    user_id: int
    username: str | None = None
    firstname: str | None = None
    lastname: str | None = None

    @property
    def fullname(self) -> str:
        return " ".join(
            part for part in (self.firstname, self.lastname) if part
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "firstname": self.firstname,
            "lastname": self.lastname,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Profile":
        return Profile(
            user_id=int(data["user_id"]),
            username=data.get("username"),
            firstname=data.get("firstname"),
            lastname=data.get("lastname"),
        )


class ResolverError(Exception):
    """Base for resolution failures raised by a raw ``PhoneResolver``."""


class FloodError(ResolverError):
    """Telegram asked us to back off for ``seconds`` (a FloodWait)."""

    def __init__(self, seconds: float) -> None:
        super().__init__("flood wait for %.0fs" % seconds)
        self.seconds = seconds


class PhoneResolver(Protocol):
    """Raw phone → profile lookup.

    Returns ``None`` when the number is definitively not reachable (no
    Telegram account, or the target's privacy hides it). Raises ``FloodError``
    on a FloodWait and any other exception on a transient failure. ``label``
    is an optional display name for the lookup side effect (e.g. the imported
    contact's name); it never affects the resolved result.
    """

    async def resolve(
        self, phone: str, label: str | None = None
    ) -> Profile | None: ...


class ResolveOutcome(Enum):
    RESOLVED = "resolved"  # a profile was found
    ABSENT = "absent"  # no Telegram / privacy closed — do not retry
    DEFERRED = "deferred"  # a guard blocked the call — retry later
    FAILED = "failed"  # the lookup errored — retry later


@dataclass(frozen=True)
class Resolution:
    outcome: ResolveOutcome
    profile: Profile | None = None

    @property
    def known(self) -> bool:
        """True once the number needs no further lookups (found or absent)."""
        return self.outcome in (ResolveOutcome.RESOLVED, ResolveOutcome.ABSENT)


@dataclass
class _CacheEntry:
    profile: Profile | None
    expires_at: float


class ProfileCache:
    """Phone → profile cache with separate TTLs for hits and misses.

    A found profile is cached for ``positive_ttl`` (identities change rarely);
    a definitive miss for the shorter ``negative_ttl``, because a person may
    join Telegram or open their privacy later and we want to pick that up.
    """

    def __init__(self, positive_ttl: float, negative_ttl: float) -> None:
        self._positive_ttl = positive_ttl
        self._negative_ttl = negative_ttl
        self._entries: dict[str, _CacheEntry] = {}

    def lookup(self, phone: str, now: float) -> Resolution | None:
        """A cached ``Resolution``, or ``None`` on a miss/expiry."""
        entry = self._entries.get(phone)
        if entry is None:
            return None
        if entry.expires_at <= now:
            del self._entries[phone]
            return None
        if entry.profile is None:
            return Resolution(ResolveOutcome.ABSENT)
        return Resolution(ResolveOutcome.RESOLVED, entry.profile)

    def put(self, phone: str, profile: Profile | None, now: float) -> None:
        ttl = self._positive_ttl if profile is not None else self._negative_ttl
        self._entries[phone] = _CacheEntry(profile, now + ttl)

    def prune(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for key in expired:
            del self._entries[key]

    def snapshot(self, now: float) -> dict[str, Any]:
        self.prune(now)
        return {
            phone: {
                "profile": entry.profile.to_dict()
                if entry.profile is not None
                else None,
                "expires_at": entry.expires_at,
            }
            for phone, entry in self._entries.items()
        }

    def restore(self, data: dict[str, Any], now: float) -> None:
        self._entries.clear()
        for phone, raw in data.items():
            expires_at = float(raw["expires_at"])
            if expires_at <= now:
                continue
            profile_raw = raw.get("profile")
            profile = (
                Profile.from_dict(profile_raw)
                if profile_raw is not None
                else None
            )
            self._entries[phone] = _CacheEntry(profile, expires_at)


class RateLimiter:
    """Token-bucket limiter with a persisted FloodWait cooldown.

    A call is allowed only when it clears every guard: it is past any active
    cooldown, at least ``min_interval`` after the previous call, and under
    both the rolling hourly and daily caps. ``trigger_cooldown`` records the
    deadline Telegram handed us (padded by ``cooldown_margin``); it is part of
    the serialized state, so a restart honours a cooldown that is still open.
    """

    def __init__(
        self,
        min_interval: float,
        per_hour: int,
        per_day: int,
        cooldown_margin: float = 0.1,
    ) -> None:
        self._min_interval = min_interval
        self._per_hour = per_hour
        self._per_day = per_day
        self._cooldown_margin = cooldown_margin
        self._calls: list[float] = []
        self._cooldown_until = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - DAY
        self._calls = [t for t in self._calls if t > cutoff]

    def cooldown_remaining(self, now: float) -> float:
        return max(0.0, self._cooldown_until - now)

    def allowed(self, now: float) -> bool:
        if now < self._cooldown_until:
            return False
        self._prune(now)
        if self._calls and now - self._calls[-1] < self._min_interval:
            return False
        if sum(1 for t in self._calls if t > now - HOUR) >= self._per_hour:
            return False
        if len(self._calls) >= self._per_day:
            return False
        return True

    def try_acquire(self, now: float) -> bool:
        """Consume one slot when a call is allowed; report whether it was."""
        if not self.allowed(now):
            return False
        self._calls.append(now)
        return True

    def trigger_cooldown(self, seconds: float, now: float) -> None:
        deadline = now + seconds * (1.0 + self._cooldown_margin)
        self._cooldown_until = max(self._cooldown_until, deadline)

    def snapshot(self, now: float) -> dict[str, Any]:
        self._prune(now)
        return {"calls": list(self._calls), "cooldown_until": self._cooldown_until}

    def restore(self, data: dict[str, Any], now: float) -> None:
        calls = data.get("calls", [])
        self._calls = [float(t) for t in calls if float(t) > now - DAY]
        self._cooldown_until = float(data.get("cooldown_until", 0.0))


class ResolverStatePort(Protocol):
    """Persistence for the cache and limiter state (a JSON blob on a volume)."""

    def load(self) -> dict[str, Any]: ...

    def save(self, document: dict[str, Any]) -> None: ...


class CachingResolver:
    """Anti-flood facade over a raw ``PhoneResolver``.

    ``cached`` answers from the cache only (no network) — used to enrich a
    message the moment it is sent. ``resolve`` runs the full path: cache, then
    the rate limiter, then the raw lookup, folding a ``FloodError`` into the
    cooldown. Nothing here raises for an ordinary miss or block; the outcome
    tells the caller whether to retry later.
    """

    def __init__(
        self,
        raw: PhoneResolver,
        cache: ProfileCache,
        limiter: RateLimiter,
        store: ResolverStatePort | None = None,
        clock: Callable[[], float] = time,
    ) -> None:
        self._raw = raw
        self._cache = cache
        self._limiter = limiter
        self._store = store
        self._clock = clock
        self._log = getLogger("default")
        self._load()

    def cached(self, phone: str) -> Resolution | None:
        """Cache-only lookup; ``None`` on a miss."""
        return self._cache.lookup(phone, self._clock())

    def cooldown_remaining(self) -> float:
        return self._limiter.cooldown_remaining(self._clock())

    async def resolve(self, phone: str, label: str | None = None) -> Resolution:
        now = self._clock()
        hit = self._cache.lookup(phone, now)
        if hit is not None:
            return hit
        if not self._limiter.try_acquire(now):
            return Resolution(ResolveOutcome.DEFERRED)

        try:
            profile = await self._raw.resolve(phone, label)
        except FloodError as err:
            self._limiter.trigger_cooldown(err.seconds, now)
            self._log.warning(
                "Resolver flood wait %.0fs, cooling down" % err.seconds
            )
            outcome = Resolution(ResolveOutcome.DEFERRED)
        except Exception as err:  # best-effort: never propagate to the caller
            self._log.warning("Resolver lookup failed for %s: %s" % (phone, err))
            outcome = Resolution(ResolveOutcome.FAILED)
        else:
            self._cache.put(phone, profile, now)
            outcome = Resolution(
                ResolveOutcome.RESOLVED if profile is not None else ResolveOutcome.ABSENT,
                profile,
            )
        self._save()
        return outcome

    def _load(self) -> None:
        if self._store is None:
            return
        try:
            document = self._store.load()
        except Exception as err:
            self._log.warning("Resolver state unreadable, ignoring: %s" % err)
            return
        now = self._clock()
        self._cache.restore(document.get("cache", {}), now)
        self._limiter.restore(document.get("limiter", {}), now)

    def _save(self) -> None:
        if self._store is None:
            return
        now = self._clock()
        document = {
            "cache": self._cache.snapshot(now),
            "limiter": self._limiter.snapshot(now),
        }
        try:
            self._store.save(document)
        except Exception as err:
            self._log.warning("Cannot persist resolver state: %s" % err)


class FileResolverStore:
    """``ResolverStatePort`` backed by an atomically-written JSON file.

    Lives on the data volume next to ``state.json`` so the cache and cooldown
    survive restarts. Writes go through a tmp file + ``rename`` so a crash
    mid-write cannot corrupt the previous snapshot; an unreadable file loads
    as empty rather than taking the resolver down.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._log = getLogger("default")
        path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            with open(self._path) as fp:
                document = json_load(fp)
        except FileNotFoundError:
            return {}
        except (JSONDecodeError, OSError) as err:
            self._log.error("Resolver state unreadable, resetting: %s" % err)
            return {}
        if not isinstance(document, dict):
            self._log.error("Resolver state has unexpected shape, resetting")
            return {}
        return document

    def save(self, document: dict[str, Any]) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w") as fp:
            json_dump(document, fp)
            fp.flush()
            fsync(fp.fileno())
        replace(tmp_path, self._path)
