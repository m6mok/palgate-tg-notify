from pathlib import Path

import pytest

from state import FileStateStore, MemoryStateStore, StateLockError


@pytest.fixture(params=["memory", "file"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> MemoryStateStore | FileStateStore:
    if request.param == "memory":
        return MemoryStateStore()
    return FileStateStore(tmp_path / "state.json")


class TestStateStoreContract:
    """CAS semantics both implementations must satisfy."""

    @pytest.mark.asyncio
    async def test_marker_is_none_initially(
        self, store: MemoryStateStore | FileStateStore
    ) -> None:
        assert await store.get_marker("gate", "telegram") is None

    @pytest.mark.asyncio
    async def test_advance_from_none_sets_marker(
        self, store: MemoryStateStore | FileStateStore
    ) -> None:
        assert await store.advance("gate", "telegram", None, "k1") is True
        assert await store.get_marker("gate", "telegram") == "k1"

    @pytest.mark.asyncio
    async def test_advance_with_wrong_expected_is_rejected(
        self, store: MemoryStateStore | FileStateStore
    ) -> None:
        await store.advance("gate", "telegram", None, "k1")

        assert await store.advance("gate", "telegram", "stale", "k2") is False
        assert await store.get_marker("gate", "telegram") == "k1"

    @pytest.mark.asyncio
    async def test_advance_with_matching_expected_moves_marker(
        self, store: MemoryStateStore | FileStateStore
    ) -> None:
        await store.advance("gate", "telegram", None, "k1")

        assert await store.advance("gate", "telegram", "k1", "k2") is True
        assert await store.get_marker("gate", "telegram") == "k2"

    @pytest.mark.asyncio
    async def test_sources_and_channels_are_isolated(
        self, store: MemoryStateStore | FileStateStore
    ) -> None:
        await store.advance("gate_a", "telegram", None, "a-tg")
        await store.advance("gate_a", "max", None, "a-max")
        await store.advance("gate_b", "telegram", None, "b-tg")

        assert await store.get_marker("gate_a", "telegram") == "a-tg"
        assert await store.get_marker("gate_a", "max") == "a-max"
        assert await store.get_marker("gate_b", "telegram") == "b-tg"
        assert await store.get_marker("gate_b", "max") is None


class TestFileStateStore:
    @pytest.mark.asyncio
    async def test_state_survives_a_new_instance(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        first = FileStateStore(path)
        await first.advance("gate", "telegram", None, "k1")

        reopened = FileStateStore(path)

        assert await reopened.get_marker("gate", "telegram") == "k1"

    @pytest.mark.asyncio
    async def test_missing_parent_directories_are_created(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "deep" / "nested" / "state.json"
        store = FileStateStore(path)

        assert await store.advance("gate", "telegram", None, "k1") is True
        assert path.exists()

    @pytest.mark.asyncio
    async def test_corrupt_state_file_resets_to_empty(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not json at all")
        store = FileStateStore(path)

        assert await store.get_marker("gate", "telegram") is None
        # The store must stay writable after a reset.
        assert await store.advance("gate", "telegram", None, "k1") is True
        assert await store.get_marker("gate", "telegram") == "k1"

    @pytest.mark.asyncio
    async def test_state_file_with_unexpected_shape_resets(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text('["not", "a", "document"]')
        store = FileStateStore(path)

        assert await store.get_marker("gate", "telegram") is None

    @pytest.mark.asyncio
    async def test_no_tmp_file_left_behind_after_write(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"
        store = FileStateStore(path)
        await store.advance("gate", "telegram", None, "k1")

        leftovers = [p.name for p in tmp_path.iterdir()]
        assert sorted(leftovers) == ["state.json"]


class TestLeaderLock:
    def test_second_instance_cannot_take_the_lock(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"
        holder = FileStateStore(path)
        holder.acquire_lock(timeout=1)
        contender = FileStateStore(path)

        with pytest.raises(StateLockError):
            contender.acquire_lock(timeout=0.3)

        holder.release_lock()

    def test_lock_is_reacquirable_after_release(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        holder = FileStateStore(path)
        holder.acquire_lock(timeout=1)
        holder.release_lock()

        successor = FileStateStore(path)
        successor.acquire_lock(timeout=0.3)
        successor.release_lock()

    def test_release_without_acquire_is_a_no_op(self, tmp_path: Path) -> None:
        FileStateStore(tmp_path / "state.json").release_lock()
