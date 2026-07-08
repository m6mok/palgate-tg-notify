from asyncio import Event, wait_for
from logging import Formatter
from os import getpid, kill
from pathlib import Path
from signal import SIGTERM
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from bot import OpsBot
from config import Settings
from main import (
    build_bot,
    build_client,
    build_logging_config,
    build_watcher,
    main,
    service_version,
)
from palgate import PalgateClient
from service import GateWatcher
from state import FileStateStore


class TestBuildLoggingConfig:
    def test_telegram_log_handler_is_wired(self, settings: Settings) -> None:
        config = build_logging_config(settings)

        handler = config["handlers"]["log"]
        assert handler["class"] == "telegram_handler.TelegramHandler"
        assert handler["token"] == settings.TELEGRAM_API_TOKEN
        assert handler["chat_id"] == settings.TELEGRAM_LOG_CHAT_ID

    def test_file_handler_rotates(self, settings: Settings) -> None:
        config = build_logging_config(settings)

        handler = config["handlers"]["file"]
        assert handler["class"] == "logging.handlers.RotatingFileHandler"
        assert handler["maxBytes"] > 0
        assert handler["backupCount"] > 0

    def test_chat_delivery_is_not_a_logger_anymore(
        self, settings: Settings
    ) -> None:
        config = build_logging_config(settings)

        assert set(config["loggers"]) == {"default", "log"}


class TestBuildWatcher:
    @pytest.mark.asyncio
    async def test_builds_a_gate_watcher_from_settings(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)

            assert isinstance(watcher, GateWatcher)


class TestBuildClient:
    @pytest.mark.asyncio
    async def test_builds_a_client_without_sessions_url(
        self, settings: Settings
    ) -> None:
        async with AsyncClient() as http:
            client = build_client(settings, http)

            assert isinstance(client, PalgateClient)
            assert client._sessions_url is None

    @pytest.mark.asyncio
    async def test_substitutes_sessions_url_placeholders(
        self, settings: Settings
    ) -> None:
        settings.URL_USER_SESSIONS = (
            "https://example.com/{user_id}/sessions/{device_id}"
        )
        async with AsyncClient() as http:
            client = build_client(settings, http)

            assert client._sessions_url == (
                "https://example.com/12345/sessions/test_device"
            )


class TestBuildBot:
    @pytest.mark.asyncio
    async def test_builds_an_ops_bot_from_settings(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)
            bot = build_bot(settings, http, watcher, client, store)

            assert isinstance(bot, OpsBot)
            assert bot._chat_id == settings.TELEGRAM_LOG_CHAT_ID


class TestServiceVersion:
    def test_returns_a_nonempty_string(self) -> None:
        assert service_version() != ""


class TestMain:
    @pytest.mark.asyncio
    async def test_main_wires_everything_and_releases_the_lock(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_mock = AsyncMock()
        bot_run_mock = AsyncMock()
        original_converter = Formatter.converter
        try:
            with (
                patch("main.Settings", return_value=settings),
                patch("main.dictConfig") as dict_config,
                patch.object(GateWatcher, "run", run_mock),
                patch.object(OpsBot, "run", bot_run_mock),
                caplog.at_level("INFO", logger="log"),
            ):
                await main()
        finally:
            Formatter.converter = original_converter

        dict_config.assert_called_once()
        run_mock.assert_awaited_once()
        bot_run_mock.assert_awaited_once()

        # Lifecycle events must reach the ops ("log") logger.
        messages = [record.message for record in caplog.records]
        assert any(
            message.startswith("Started palgate-tg-notify")
            for message in messages
        )
        assert "Shut down cleanly" in messages

        # The leader lock must be free again after a clean shutdown.
        successor = FileStateStore(Path(settings.STATE_FILE))
        successor.acquire_lock(timeout=0.5)
        successor.release_lock()

    @pytest.mark.asyncio
    async def test_sigterm_stops_the_loop_and_is_reported(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def signal_driven_run(self: GateWatcher, stop: Event) -> None:
            kill(getpid(), SIGTERM)
            await wait_for(stop.wait(), timeout=5)

        original_converter = Formatter.converter
        try:
            with (
                patch("main.Settings", return_value=settings),
                patch("main.dictConfig"),
                patch.object(GateWatcher, "run", signal_driven_run),
                patch.object(OpsBot, "run", AsyncMock()),
                caplog.at_level("INFO", logger="log"),
            ):
                await main()
        finally:
            Formatter.converter = original_converter

        assert any(
            "Received SIGTERM, shutting down" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_crash_is_reported_to_the_ops_chat_and_reraised(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_mock = AsyncMock(side_effect=RuntimeError("boom"))
        original_converter = Formatter.converter
        try:
            with (
                patch("main.Settings", return_value=settings),
                patch("main.dictConfig"),
                patch.object(GateWatcher, "run", run_mock),
                patch.object(OpsBot, "run", AsyncMock()),
                caplog.at_level("ERROR", logger="log"),
            ):
                with pytest.raises(RuntimeError, match="boom"):
                    await main()
        finally:
            Formatter.converter = original_converter

        assert any(
            "Service crashed" in record.message for record in caplog.records
        )

        # Even after a crash the leader lock must not leak.
        successor = FileStateStore(Path(settings.STATE_FILE))
        successor.acquire_lock(timeout=0.5)
        successor.release_lock()
