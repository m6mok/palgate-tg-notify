from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from json import JSONDecodeError, dump as json_dump, load as json_load
from logging import getLogger
from os import fsync, replace
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Protocol, TextIO

STATE_VERSION = 1


class StateLockError(Exception):
    """The single-writer lock could not be acquired in time."""


class StateStore(Protocol):
    """Per-(source, channel) markers of the last delivered log entry.

    ``advance`` is compare-and-swap: the marker moves only if it still equals
    ``expected``, so concurrent writers cannot silently overwrite each other.
    """

    async def get_marker(self, source: str, channel: str) -> str | None: ...

    async def advance(
        self, source: str, channel: str, expected: str | None, new: str
    ) -> bool: ...


class MemoryStateStore:
    """In-memory store for tests and throwaway runs; lost on restart."""

    def __init__(self) -> None:
        self._markers: dict[tuple[str, str], str] = {}

    async def get_marker(self, source: str, channel: str) -> str | None:
        return self._markers.get((source, channel))

    async def advance(
        self, source: str, channel: str, expected: str | None, new: str
    ) -> bool:
        if self._markers.get((source, channel)) != expected:
            return False
        self._markers[(source, channel)] = new
        return True


class FileStateStore:
    """Markers in a JSON file guarded by an exclusive flock leader lock.

    The lock is held for the whole process lifetime: a replacement container
    started during a deploy waits in ``acquire_lock`` until the previous one
    shuts down, so there is never more than one writer per state file.
    Writes are atomic (tmp file + rename) — a crash mid-write cannot corrupt
    the previous state.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._lock_file: TextIO | None = None
        self._log = getLogger("default")
        path.parent.mkdir(parents=True, exist_ok=True)

    def acquire_lock(self, timeout: float = 60) -> None:
        lock_file = open(self._lock_path, "w")
        deadline = monotonic() + timeout
        while True:
            try:
                flock(lock_file.fileno(), LOCK_EX | LOCK_NB)
            except BlockingIOError:
                if monotonic() >= deadline:
                    lock_file.close()
                    raise StateLockError(
                        "State is locked by another instance: %s" % self._lock_path
                    )
                sleep(0.2)
            else:
                self._lock_file = lock_file
                return

    def release_lock(self) -> None:
        if self._lock_file is None:
            return
        flock(self._lock_file.fileno(), LOCK_UN)
        self._lock_file.close()
        self._lock_file = None

    async def get_marker(self, source: str, channel: str) -> str | None:
        channels = self._channels(self._read(), source)
        marker = channels.get(channel, {}).get("last_key")
        return marker if isinstance(marker, str) else None

    async def advance(
        self, source: str, channel: str, expected: str | None, new: str
    ) -> bool:
        document = self._read()
        channels = self._channels(document, source)
        current = channels.get(channel, {}).get("last_key")
        if current != expected:
            return False
        channels[channel] = {"last_key": new}
        self._write(document)
        return True

    @staticmethod
    def _channels(document: dict[str, Any], source: str) -> dict[str, Any]:
        source_state: dict[str, Any] = document["sources"].setdefault(source, {})
        channels: dict[str, Any] = source_state.setdefault("channels", {})
        return channels

    def _read(self) -> dict[str, Any]:
        try:
            with open(self._path) as fp:
                document = json_load(fp)
        except FileNotFoundError:
            return self._empty()
        except (JSONDecodeError, OSError) as err:
            # A corrupt state file must not kill the service: restart from
            # empty markers — worst case is a burst of repeated notifications.
            self._log.error("State file is unreadable, resetting: %s" % err)
            return self._empty()

        if not isinstance(document, dict) or not isinstance(
            document.get("sources"), dict
        ):
            self._log.error("State file has unexpected shape, resetting")
            return self._empty()
        return document

    def _write(self, document: dict[str, Any]) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w") as fp:
            json_dump(document, fp)
            fp.flush()
            fsync(fp.fileno())
        replace(tmp_path, self._path)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": STATE_VERSION, "sources": {}}
