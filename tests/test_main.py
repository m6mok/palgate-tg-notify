from asyncio import Event, wait_for
from importlib.metadata import PackageNotFoundError
from logging import INFO, Formatter, LogRecord
from os import getpid, kill
from pathlib import Path
from signal import SIGTERM
from tomllib import load as toml_load
from unittest.mock import AsyncMock, patch

import pytest
from aiologging import (
    AsyncTelegramHandler,
    getLogger as aio_get_logger,
    shutdown as aio_shutdown,
)
from httpx import AsyncClient

from bot import OpsBot
from config import Settings
from github_client import GithubClient
from main import (
    RolePrefixFilter,
    build_bot,
    build_client,
    build_enrichment,
    build_logging_config,
    build_telegram_log_handler,
    build_watcher,
    configure_logging,
    main,
    read_stored_version,
    service_version,
    store_version,
    version_transition,
)
from enrich import Enricher
from notify import TelegramNotifier
from palgate import PalgateClient
from service import GateWatcher
from state import FileStateStore


class TestBuildLoggingConfig:
    def test_log_handler_bridges_into_aiologging(self) -> None:
        config = build_logging_config()

        handler = config["handlers"]["log"]
        assert handler["class"] == "aiologging.bridge.StdlibBridgeHandler"

    def test_file_handler_rotates(self) -> None:
        config = build_logging_config()

        handler = config["handlers"]["file"]
        assert handler["class"] == "logging.handlers.RotatingFileHandler"
        assert handler["maxBytes"] > 0
        assert handler["backupCount"] > 0

    def test_chat_delivery_is_not_a_logger_anymore(self) -> None:
        config = build_logging_config()

        assert set(config["loggers"]) == {"default", "log"}

    def test_prod_role_adds_no_prefix_filter(self) -> None:
        config = build_logging_config()

        assert "filters" not in config
        assert "filters" not in config["loggers"]["log"]

    def test_prestable_role_prefixes_the_ops_log(self) -> None:
        config = build_logging_config("prestable")

        assert config["loggers"]["log"]["filters"] == ["role"]
        role_filter = config["filters"]["role"]
        assert role_filter["()"] is RolePrefixFilter
        assert role_filter["role"] == "prestable"


class TestRolePrefixFilter:
    def make_record(self, msg: str, *args: object) -> LogRecord:
        return LogRecord(
            name="log",
            level=INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args or None,
            exc_info=None,
        )

    def test_prefixes_the_rendered_message(self) -> None:
        record = self.make_record("Updated %s → %s", "2.3.1", "2.4.0")
        keep = RolePrefixFilter("prestable").filter(record)

        assert keep is True
        assert record.getMessage() == "[prestable] Updated 2.3.1 → 2.4.0"

    def test_plain_message_is_prefixed_verbatim(self) -> None:
        record = self.make_record("Shut down cleanly")
        RolePrefixFilter("prestable").filter(record)

        assert record.getMessage() == "[prestable] Shut down cleanly"


class TestTelegramLogDelivery:
    def test_handler_targets_the_ops_chat(self, settings: Settings) -> None:
        handler = build_telegram_log_handler(settings)

        assert handler.token == settings.TELEGRAM_API_TOKEN
        assert handler.chat_id == settings.TELEGRAM_LOG_CHAT_ID
        assert handler.parse_mode == "HTML"

    @pytest.mark.asyncio
    async def test_configure_logging_attaches_the_telegram_handler(
        self, settings: Settings
    ) -> None:
        # dictConfig is patched so the test process keeps its own
        # logging tree; the aiologging side is exercised for real.
        try:
            with patch("main.dictConfig") as dict_config:
                configure_logging(settings)

            dict_config.assert_called_once()
            telegram_log = aio_get_logger("log")
            assert any(
                isinstance(handler, AsyncTelegramHandler)
                for handler in telegram_log.handlers
            )
        finally:
            await aio_shutdown(timeout=5.0)


class TestBuildEnrichment:
    def test_disabled_by_default(self, settings: Settings) -> None:
        assert build_enrichment(settings) is None

    def test_enabled_without_credentials_is_disabled(
        self, settings: Settings
    ) -> None:
        settings = Settings(
            **{**settings.model_dump(), "RESOLVE_ENABLED": True}
        )
        assert build_enrichment(settings) is None

    def test_wired_when_enabled_with_credentials(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        settings = Settings(
            **{
                **settings.model_dump(),
                "RESOLVE_ENABLED": True,
                "TG_API_ID": 12345,
                "TG_API_HASH": "deadbeef",
                "TG_SESSION": str(tmp_path / "tele"),
                "RESOLVER_STATE_FILE": str(tmp_path / "resolver.json"),
            }
        )

        result = build_enrichment(settings)

        assert result is not None
        enricher, adapter = result
        assert isinstance(enricher, Enricher)
        adapter._client.session.close()  # release the sqlite session file

    def test_string_session_takes_precedence_over_file(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        from telethon.crypto import AuthKey
        from telethon.sessions import StringSession

        blob_session = StringSession()
        blob_session.set_dc(1, "149.154.167.50", 443)
        blob_session.auth_key = AuthKey(bytes(256))
        blob = blob_session.save()
        settings = Settings(
            **{
                **settings.model_dump(),
                "RESOLVE_ENABLED": True,
                "TG_API_ID": 12345,
                "TG_API_HASH": "deadbeef",
                "TG_SESSION": str(tmp_path / "tele"),
                "TG_SESSION_STRING": blob,
                "RESOLVER_STATE_FILE": str(tmp_path / "resolver.json"),
            }
        )

        result = build_enrichment(settings)

        assert result is not None
        # No on-disk session file is created when a StringSession is supplied.
        assert not (tmp_path / "tele.session").exists()


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

    @pytest.mark.asyncio
    async def test_enricher_is_passed_to_the_watcher(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            sentinel = object()
            watcher = build_watcher(
                settings, http, store, client, enricher=sentinel  # type: ignore[arg-type]
            )

            assert watcher._enricher is sentinel

    @pytest.mark.asyncio
    async def test_max_channel_is_off_by_default(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)

            assert [n.name for n in watcher._notifiers] == ["telegram"]

    @pytest.mark.asyncio
    async def test_max_channel_is_wired_when_token_is_set(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        settings = Settings(
            **{
                **settings.model_dump(),
                "MAX_API_TOKEN": "max_token",
                "MAX_CHAT_ID": 77,
            }
        )
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)

            assert [n.name for n in watcher._notifiers] == ["telegram", "max"]


class TestBuildClient:
    @pytest.mark.asyncio
    async def test_builds_a_palgate_client_from_settings(
        self, settings: Settings
    ) -> None:
        async with AsyncClient() as http:
            client = build_client(settings, http)

            assert isinstance(client, PalgateClient)


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

    @pytest.mark.asyncio
    async def test_rollback_is_off_without_a_github_token(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)
            bot = build_bot(settings, http, watcher, client, store)

            assert bot._github is None

    @pytest.mark.asyncio
    async def test_rollback_is_wired_when_a_github_token_is_set(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        settings = Settings(
            **{**settings.model_dump(), "GITHUB_TOKEN": "gh_token"}
        )
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)
            bot = build_bot(settings, http, watcher, client, store)

            assert isinstance(bot._github, GithubClient)

    @pytest.mark.asyncio
    async def test_mock_is_off_without_a_prestable_chat_id(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)
            bot = build_bot(settings, http, watcher, client, store)

            assert bot._mock_notifier is None

    @pytest.mark.asyncio
    async def test_mock_is_wired_when_a_prestable_chat_id_is_set(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        settings = Settings(
            **{
                **settings.model_dump(),
                "PRESTABLE_TELEGRAM_CHAT_ID": -100123,
            }
        )
        store = FileStateStore(tmp_path / "state.json")
        async with AsyncClient() as http:
            client = build_client(settings, http)
            watcher = build_watcher(settings, http, store, client)
            bot = build_bot(settings, http, watcher, client, store)

            assert isinstance(bot._mock_notifier, TelegramNotifier)
            assert bot._mock_notifier._chat_id == -100123


class TestServiceVersion:
    def test_returns_a_nonempty_string(self) -> None:
        assert service_version() != ""

    def test_falls_back_to_pyproject_when_dist_is_missing(self) -> None:
        pyproject = Path(__file__).parents[1] / "pyproject.toml"
        with pyproject.open("rb") as file:
            expected = toml_load(file)["project"]["version"]

        with patch("main.version", side_effect=PackageNotFoundError):
            assert service_version() == expected

    def test_unknown_when_nothing_is_available(self, tmp_path: Path) -> None:
        with (
            patch("main.version", side_effect=PackageNotFoundError),
            patch("main._PYPROJECT_PATHS", (tmp_path / "pyproject.toml",)),
        ):
            assert service_version() == "unknown"

    def test_pyproject_without_version_field_is_skipped(
        self, tmp_path: Path
    ) -> None:
        broken = tmp_path / "pyproject.toml"
        broken.write_text('[project]\nname = "x"\n')

        with (
            patch("main.version", side_effect=PackageNotFoundError),
            patch("main._PYPROJECT_PATHS", (broken,)),
        ):
            assert service_version() == "unknown"


class TestVersionTransition:
    def test_first_boot_is_silent(self) -> None:
        assert version_transition(None, "2.0.0") is None

    def test_same_version_is_silent(self) -> None:
        assert version_transition("2.0.0", "2.0.0") is None

    def test_upgrade_is_reported(self) -> None:
        assert version_transition("0.4.0", "2.0.0") == "Updated 0.4.0 → 2.0.0"

    def test_downgrade_is_reported_as_rollback(self) -> None:
        assert (
            version_transition("2.1.0", "2.0.0") == "Rolled back 2.1.0 → 2.0.0"
        )

    def test_comparison_is_numeric_not_lexicographic(self) -> None:
        assert (
            version_transition("2.9.0", "2.10.0") == "Updated 2.9.0 → 2.10.0"
        )

    def test_unparsable_version_falls_back_to_updated(self) -> None:
        assert (
            version_transition("unknown", "2.0.0") == "Updated unknown → 2.0.0"
        )


class TestVersionFile:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "version"

        store_version(path, "2.0.0")

        assert read_stored_version(path) == "2.0.0"

    def test_missing_file_reads_as_none(self, tmp_path: Path) -> None:
        assert read_stored_version(tmp_path / "version") is None

    def test_empty_file_reads_as_none(self, tmp_path: Path) -> None:
        path = tmp_path / "version"
        path.write_text("\n")

        assert read_stored_version(path) is None


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
    async def test_prestable_role_runs_without_the_ops_bot(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = Settings(
            **{**settings.model_dump(), "SERVICE_ROLE": "prestable"}
        )
        run_mock = AsyncMock()
        bot_run_mock = AsyncMock()
        original_converter = Formatter.converter
        try:
            with (
                patch("main.Settings", return_value=settings),
                patch("main.dictConfig"),
                patch.object(GateWatcher, "run", run_mock),
                patch.object(OpsBot, "run", bot_run_mock),
                caplog.at_level("INFO", logger="log"),
            ):
                await main()
        finally:
            Formatter.converter = original_converter

        run_mock.assert_awaited_once()
        bot_run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_change_is_announced_once(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        store_version(Path(settings.VERSION_FILE), "0.3.0")
        original_converter = Formatter.converter
        try:
            with (
                patch("main.Settings", return_value=settings),
                patch("main.dictConfig"),
                patch.object(GateWatcher, "run", AsyncMock()),
                patch.object(OpsBot, "run", AsyncMock()),
                caplog.at_level("INFO", logger="log"),
            ):
                await main()
                first_run = [record.message for record in caplog.records]
                caplog.clear()
                await main()
                second_run = [record.message for record in caplog.records]
        finally:
            Formatter.converter = original_converter

        expected = "Updated 0.3.0 → %s" % service_version()
        assert expected in first_run
        assert expected not in second_run
        assert (
            read_stored_version(Path(settings.VERSION_FILE))
            == service_version()
        )

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
