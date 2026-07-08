from asyncio import Event, create_task, sleep, wait_for
from pathlib import Path
from time import time
from typing import Any, List, Sequence

import pytest

from models import LogItem
from notify import NotifyError
from palgate import AuthError, TransientFetchError
from service import GateWatcher, item_key
from state import MemoryStateStore
from tests.conftest import (
    BASE_LOG_ITEM_DATA,
    SECOND_LOG_ITEM_DATA,
    THIRD_LOG_ITEM_DATA,
    RecordingNotifier,
    ScriptedPalgateClient,
    make_response,
)


def make_watcher(
    script: List[Any],
    notifiers: Sequence[RecordingNotifier] | None = None,
    store: MemoryStateStore | None = None,
    heartbeat_path: Path | None = None,
    alert_after: int = 10,
    cron_delay: float = 0,
) -> tuple[GateWatcher, ScriptedPalgateClient, RecordingNotifier]:
    client = ScriptedPalgateClient(script)
    notifier = RecordingNotifier(name="telegram")
    watcher = GateWatcher(
        source="gate",
        client=client,  # type: ignore[arg-type]
        store=store if store is not None else MemoryStateStore(),
        notifiers=notifiers if notifiers is not None else (notifier,),
        cron_delay=cron_delay,
        max_backoff=0,
        alert_after=alert_after,
        heartbeat_path=heartbeat_path,
    )
    return watcher, client, notifier


class TestItemKey:
    def test_key_combines_time_and_phone(self) -> None:
        item = LogItem.model_validate(BASE_LOG_ITEM_DATA)

        assert item_key(item) == "1708675200:79001234567"

    def test_key_falls_back_to_user_id_without_sn(self) -> None:
        item = LogItem.model_validate({**BASE_LOG_ITEM_DATA, "sn": ""})

        assert item_key(item) == "1708675200:12345"

    def test_key_ignores_mutable_presentation_fields(self) -> None:
        item = LogItem.model_validate(BASE_LOG_ITEM_DATA)
        renamed = LogItem.model_validate(
            {**BASE_LOG_ITEM_DATA, "firstname": "Renamed"}
        )

        assert item_key(item) == item_key(renamed)


class TestFirstPoll:
    @pytest.mark.asyncio
    async def test_first_poll_primes_marker_without_notifying(self) -> None:
        store = MemoryStateStore()
        watcher, _, notifier = make_watcher(
            [make_response(BASE_LOG_ITEM_DATA)], store=store
        )

        assert await watcher.poll_once() is True

        assert notifier.sent == []
        marker = await store.get_marker("gate", "telegram")
        assert marker == "1708675200:79001234567"


class TestDelivery:
    @pytest.mark.asyncio
    async def test_no_new_items_means_no_sends(self) -> None:
        response = make_response(BASE_LOG_ITEM_DATA)
        watcher, _, notifier = make_watcher([response, response])

        await watcher.poll_once()
        await watcher.poll_once()

        assert notifier.sent == []

    @pytest.mark.asyncio
    async def test_new_item_is_delivered_and_marker_advances(self) -> None:
        store = MemoryStateStore()
        watcher, _, notifier = make_watcher(
            [
                make_response(BASE_LOG_ITEM_DATA),
                make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA),
            ],
            store=store,
        )

        await watcher.poll_once()
        assert await watcher.poll_once() is True

        assert len(notifier.sent) == 1
        assert "Jane Smith" in notifier.sent[0]
        marker = await store.get_marker("gate", "telegram")
        assert marker == "1708675300:79009876543"

    @pytest.mark.asyncio
    async def test_batch_is_delivered_oldest_first(self) -> None:
        watcher, _, notifier = make_watcher(
            [
                make_response(BASE_LOG_ITEM_DATA),
                make_response(
                    THIRD_LOG_ITEM_DATA,
                    SECOND_LOG_ITEM_DATA,
                    BASE_LOG_ITEM_DATA,
                ),
            ]
        )

        await watcher.poll_once()
        await watcher.poll_once()

        lines = notifier.sent[0].split("\n")
        assert len(lines) == 2
        assert "Jane Smith" in lines[0]
        assert "Bob Johnson" in lines[1]

    @pytest.mark.asyncio
    async def test_everything_is_new_when_marker_left_the_page(self) -> None:
        store = MemoryStateStore()
        await store.advance("gate", "telegram", None, "0:gone")
        watcher, _, notifier = make_watcher(
            [make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA)],
            store=store,
        )

        await watcher.poll_once()

        assert len(notifier.sent[0].split("\n")) == 2

    @pytest.mark.asyncio
    async def test_failed_delivery_keeps_marker_and_retries(self) -> None:
        store = MemoryStateStore()
        new_state = make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA)
        watcher, _, notifier = make_watcher(
            [make_response(BASE_LOG_ITEM_DATA), new_state, new_state],
            store=store,
        )
        await watcher.poll_once()

        notifier.fail_with = NotifyError("telegram down")
        assert await watcher.poll_once() is False
        assert await store.get_marker("gate", "telegram") == (
            "1708675200:79001234567"
        )

        # at-least-once: the same batch is redelivered on the next cycle
        notifier.fail_with = None
        assert await watcher.poll_once() is True
        assert len(notifier.sent) == 1
        assert "Jane Smith" in notifier.sent[0]

    @pytest.mark.asyncio
    async def test_permanently_rejected_batch_is_skipped(self) -> None:
        store = MemoryStateStore()
        watcher, _, notifier = make_watcher(
            [
                make_response(BASE_LOG_ITEM_DATA),
                make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA),
            ],
            store=store,
        )
        await watcher.poll_once()

        notifier.fail_with = NotifyError("bad message", permanent=True)
        assert await watcher.poll_once() is True

        # The poison batch must not block the channel forever.
        assert await store.get_marker("gate", "telegram") == (
            "1708675300:79009876543"
        )

    @pytest.mark.asyncio
    async def test_channels_advance_independently(self) -> None:
        store = MemoryStateStore()
        healthy = RecordingNotifier(name="max")
        failing = RecordingNotifier(name="telegram")
        watcher, _, _ = make_watcher(
            [
                make_response(BASE_LOG_ITEM_DATA),
                make_response(SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA),
            ],
            notifiers=(failing, healthy),
            store=store,
        )
        await watcher.poll_once()

        failing.fail_with = NotifyError("telegram down")
        assert await watcher.poll_once() is False

        assert len(healthy.sent) == 1
        assert await store.get_marker("gate", "max") == (
            "1708675300:79009876543"
        )
        assert await store.get_marker("gate", "telegram") == (
            "1708675200:79001234567"
        )


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_loop_exits_when_stop_is_already_set(self) -> None:
        watcher, client, _ = make_watcher([])
        stop = Event()
        stop.set()

        await wait_for(watcher.run(stop), timeout=1)

        assert client.calls == 0

    @pytest.mark.asyncio
    async def test_loop_survives_palgate_errors(self) -> None:
        watcher, client, _ = make_watcher(
            [
                TransientFetchError("boom"),
                AuthError("rejected", status_code=403),
            ]
        )
        stop = Event()
        client.on_empty = stop.set

        await wait_for(watcher.run(stop), timeout=2)

        assert client.calls == 3

    @pytest.mark.asyncio
    async def test_loop_survives_unexpected_exceptions(self) -> None:
        watcher, client, _ = make_watcher([RuntimeError("bug in the code")])
        stop = Event()
        client.on_empty = stop.set

        await wait_for(watcher.run(stop), timeout=2)

        assert client.calls == 2

    @pytest.mark.asyncio
    async def test_alert_is_escalated_after_repeated_failures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        watcher, client, _ = make_watcher(
            [TransientFetchError("boom")], alert_after=2
        )
        stop = Event()
        client.on_empty = stop.set

        with caplog.at_level("ERROR", logger="log"):
            await wait_for(watcher.run(stop), timeout=2)

        alerts = [
            record
            for record in caplog.records
            if "failing for 2 cycles" in record.message
        ]
        assert len(alerts) == 1

    @pytest.mark.asyncio
    async def test_recovery_after_failures_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        response = make_response(BASE_LOG_ITEM_DATA)
        watcher, client, _ = make_watcher(
            [
                TransientFetchError("boom"),
                TransientFetchError("boom"),
                response,
            ],
            alert_after=2,
        )
        stop = Event()
        client.on_empty = stop.set

        with caplog.at_level("INFO", logger="log"):
            await wait_for(watcher.run(stop), timeout=2)

        assert any(
            "Recovered after 2 failed cycles" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_heartbeat_is_written_each_cycle(
        self, tmp_path: Path
    ) -> None:
        heartbeat = tmp_path / "heartbeat"
        watcher, client, _ = make_watcher(
            [make_response(BASE_LOG_ITEM_DATA)], heartbeat_path=heartbeat
        )
        stop = Event()
        client.on_empty = stop.set

        await wait_for(watcher.run(stop), timeout=2)

        deadline = float(heartbeat.read_text())
        assert deadline > time()

    @pytest.mark.asyncio
    async def test_unwritable_heartbeat_does_not_kill_the_loop(self) -> None:
        heartbeat = Path("/dev/null/impossible/heartbeat")
        watcher, client, _ = make_watcher(
            [make_response(BASE_LOG_ITEM_DATA)], heartbeat_path=heartbeat
        )
        stop = Event()
        client.on_empty = stop.set

        await wait_for(watcher.run(stop), timeout=2)

        assert client.calls == 2

    @pytest.mark.asyncio
    async def test_heartbeat_failure_alerts_ops_chat_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        heartbeat = Path("/dev/null/impossible/heartbeat")
        response = make_response(BASE_LOG_ITEM_DATA)
        watcher, client, _ = make_watcher(
            [response, response, response], heartbeat_path=heartbeat
        )
        stop = Event()
        client.on_empty = stop.set

        with caplog.at_level("ERROR"):
            await wait_for(watcher.run(stop), timeout=2)

        heartbeat_errors = [
            record
            for record in caplog.records
            if "Cannot write heartbeat" in record.message
        ]
        # Every failure is visible locally, but only the first one goes
        # to the ops chat.
        assert len(heartbeat_errors) >= 2
        assert [r.name for r in heartbeat_errors].count("log") == 1

    @pytest.mark.asyncio
    async def test_poke_cuts_the_poll_delay_short(self) -> None:
        response = make_response(BASE_LOG_ITEM_DATA)
        watcher, client, _ = make_watcher(
            [response, response], cron_delay=30
        )
        stop = Event()
        client.on_empty = stop.set

        task = create_task(watcher.run(stop))
        await sleep(0.05)
        assert client.calls == 1  # sleeping out the 30s cron delay

        watcher.poke()
        await sleep(0.05)
        assert client.calls == 2

        watcher.poke()  # third cycle drains the script and sets stop
        await wait_for(task, timeout=1)

    @pytest.mark.asyncio
    async def test_pause_skips_polling_until_resume(self) -> None:
        watcher, client, _ = make_watcher([make_response(BASE_LOG_ITEM_DATA)])
        stop = Event()
        client.on_empty = stop.set

        assert watcher.pause() is True
        assert watcher.pause() is False

        task = create_task(watcher.run(stop))
        await sleep(0.05)
        assert client.calls == 0
        assert watcher.status().paused is True

        assert watcher.resume() is True
        assert watcher.resume() is False
        await wait_for(task, timeout=1)
        assert client.calls >= 1

    @pytest.mark.asyncio
    async def test_poke_polls_once_even_while_paused(self) -> None:
        watcher, client, _ = make_watcher([make_response(BASE_LOG_ITEM_DATA)])
        stop = Event()
        watcher.pause()

        task = create_task(watcher.run(stop))
        await sleep(0.05)
        assert client.calls == 0

        watcher.poke()
        await sleep(0.05)
        assert client.calls == 1
        assert watcher.status().paused is True

        stop.set()
        watcher.poke()  # wake the sleep so the loop can observe stop
        await wait_for(task, timeout=1)
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_status_snapshot_reflects_the_loop(self) -> None:
        watcher, client, _ = make_watcher([make_response(BASE_LOG_ITEM_DATA)])
        stop = Event()
        client.on_empty = stop.set

        before = watcher.status()
        assert before.started_at is None
        assert before.last_poll_at is None
        assert before.channels == ("telegram",)

        await wait_for(watcher.run(stop), timeout=2)

        after = watcher.status()
        assert after.source == "gate"
        assert after.started_at is not None
        assert after.last_ok_at is not None
        assert after.next_poll_at is not None
        # the last scripted cycle fails (script exhausted), so the
        # failure counter must be visible in the snapshot
        assert after.failures == 1
        assert after.last_poll_at >= after.last_ok_at

    @pytest.mark.asyncio
    async def test_heartbeat_restore_is_reported(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        heartbeat = tmp_path / "heartbeat"
        response = make_response(BASE_LOG_ITEM_DATA)
        watcher, client, _ = make_watcher(
            [response, response], heartbeat_path=heartbeat
        )
        stop = Event()
        client.on_empty = stop.set
        watcher._heartbeat_ok = False  # simulate an earlier write failure

        with caplog.at_level("INFO", logger="log"):
            await wait_for(watcher.run(stop), timeout=2)

        assert any(
            "Heartbeat restored" in record.message
            for record in caplog.records
        )
