from logging import Formatter
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from config import Settings
from main import build_logging_config, build_watcher, main
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
            watcher = build_watcher(settings, http, store)

            assert isinstance(watcher, GateWatcher)


class TestMain:
    @pytest.mark.asyncio
    async def test_main_wires_everything_and_releases_the_lock(
        self, settings: Settings
    ) -> None:
        run_mock = AsyncMock()
        original_converter = Formatter.converter
        try:
            with (
                patch("main.Settings", return_value=settings),
                patch("main.dictConfig") as dict_config,
                patch.object(GateWatcher, "run", run_mock),
            ):
                await main()
        finally:
            Formatter.converter = original_converter

        dict_config.assert_called_once()
        run_mock.assert_awaited_once()

        # The leader lock must be free again after a clean shutdown.
        successor = FileStateStore(Path(settings.STATE_FILE))
        successor.acquire_lock(timeout=0.5)
        successor.release_lock()
