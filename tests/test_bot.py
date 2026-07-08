from asyncio import Event, create_task, sleep, wait_for
from datetime import timedelta, timezone
from typing import Any, Callable, Dict, List

import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Request, Response

import bot as bot_module
from bot import OpsBot, format_duration
from notify import NotifyError
from palgate import TransientFetchError
from service import GateWatcher
from state import MemoryStateStore
from tests.conftest import (
    BASE_LOG_ITEM_DATA,
    SECOND_LOG_ITEM_DATA,
    RecordingNotifier,
    ScriptedPalgateClient,
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


def make_bot(
    batches: List[Any],
    watcher_script: List[Any] | None = None,
    client_script: List[Any] | None = None,
    store: MemoryStateStore | None = None,
    username: str | None = BOT_USERNAME,
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
