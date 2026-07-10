from asyncio import Event, gather, get_running_loop, run as asyncio_run
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from tomllib import TOMLDecodeError, load as toml_load
from logging import DEBUG, Formatter, getLogger
from logging.config import dictConfig
from pathlib import Path
from signal import SIGINT, SIGTERM, Signals
from typing import Any

from aiologging import (
    AsyncTelegramHandler,
    TelegramHtmlFormatter,
    getLogger as aio_get_logger,
    shutdown as aio_shutdown,
)
from httpx import AsyncClient

from bot import OpsBot
from config import Settings
from enrich import Enricher
from github_client import GithubClient
from notify import MaxNotifier, Notifier, TelegramNotifier
from palgate import PalgateClient
from resolver import (
    CachingResolver,
    FileResolverStore,
    ProfileCache,
    RateLimiter,
)
from service import GateWatcher
from state import FileStateStore
from telegram_resolver import TelegramContactResolver
from telethon.sessions import StringSession


def build_logging_config() -> dict[str, Any]:
    # stdout and the rotating file stay on stdlib handlers; only "log"
    # records cross into aiologging (via the bridge), where the ops-chat
    # HTTP delivery runs off the event loop thread.
    return {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(levelname)s][%(asctime)s] %(name)s: %(message)s",
            },
        },
        "handlers": {
            "log": {
                "class": "aiologging.bridge.StdlibBridgeHandler",
            },
            "stdout": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "palgate.log",
                "maxBytes": 5_000_000,
                "backupCount": 3,
                "formatter": "default",
            },
        },
        "loggers": {
            "default": {"handlers": ["stdout", "file"], "level": "DEBUG"},
            "log": {"handlers": ["log", "stdout", "file"], "level": "DEBUG"},
        },
    }


def build_telegram_log_handler(settings: Settings) -> AsyncTelegramHandler:
    return AsyncTelegramHandler(
        token=settings.TELEGRAM_API_TOKEN,
        chat_id=settings.TELEGRAM_LOG_CHAT_ID,
        parse_mode="HTML",
        formatter=TelegramHtmlFormatter(),
        timeout=5.0,
        backend="httpx",
    )


def configure_logging(settings: Settings) -> None:
    dictConfig(build_logging_config())
    telegram_log = aio_get_logger("log")
    telegram_log.setLevel(DEBUG)
    telegram_log.addHandler(build_telegram_log_handler(settings))


def build_client(settings: Settings, http: AsyncClient) -> PalgateClient:
    return PalgateClient(
        http=http,
        url=settings.URL_USER_LOG.format(device_id=settings.DEVICE_ID),
        session_token=settings.session_token_bytes,
        user_id=settings.USER_ID,
        token_type=settings.SESSION_TOKEN_TYPE,
    )


def build_watcher(
    settings: Settings,
    http: AsyncClient,
    store: FileStateStore,
    client: PalgateClient,
    enricher: Enricher | None = None,
) -> GateWatcher:
    notifiers: tuple[Notifier, ...] = (
        TelegramNotifier(
            http=http,
            token=settings.TELEGRAM_API_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
        ),
    )
    if settings.MAX_API_TOKEN:
        notifiers += (
            MaxNotifier(
                http=http,
                token=settings.MAX_API_TOKEN,
                chat_id=settings.MAX_CHAT_ID,
            ),
        )
    return GateWatcher(
        source=settings.DEVICE_ID,
        client=client,
        store=store,
        notifiers=notifiers,
        cron_delay=settings.CRON_DELAY,
        max_backoff=settings.MAX_BACKOFF,
        alert_after=settings.ALERT_AFTER_FAILURES,
        heartbeat_path=Path(settings.HEARTBEAT_FILE),
        enricher=enricher,
    )


def build_enrichment(
    settings: Settings,
) -> tuple[Enricher, TelegramContactResolver] | None:
    """Wire the Telegram identity enricher, or None when it stays off.

    Returns the enricher and the underlying Telethon adapter (whose session
    still has to be connected by the caller). None when the feature is
    disabled or the API credentials are missing.
    """
    if not settings.RESOLVE_ENABLED:
        return None
    if not (settings.TG_API_ID and settings.TG_API_HASH):
        getLogger("log").error(
            "RESOLVE_ENABLED is set but TG_API_ID/TG_API_HASH are missing; "
            "enrichment disabled"
        )
        return None
    # A StringSession blob (TG_SESSION_STRING) beats the on-disk session file,
    # so a headless server can carry the whole session in its env file.
    session: str | StringSession = (
        StringSession(settings.TG_SESSION_STRING)
        if settings.TG_SESSION_STRING
        else settings.TG_SESSION
    )
    adapter = TelegramContactResolver.build(
        session, settings.TG_API_ID, settings.TG_API_HASH
    )
    resolver = CachingResolver(
        raw=adapter,
        cache=ProfileCache(
            positive_ttl=settings.RESOLVE_POSITIVE_TTL,
            negative_ttl=settings.RESOLVE_NEGATIVE_TTL,
        ),
        limiter=RateLimiter(
            min_interval=settings.RESOLVE_MIN_INTERVAL,
            per_hour=settings.RESOLVE_PER_HOUR,
            per_day=settings.RESOLVE_PER_DAY,
        ),
        store=FileResolverStore(Path(settings.RESOLVER_STATE_FILE)),
    )
    enricher = Enricher(
        resolver, poll_interval=settings.RESOLVE_POLL_INTERVAL
    )
    return enricher, adapter


def build_bot(
    settings: Settings,
    http: AsyncClient,
    watcher: GateWatcher,
    client: PalgateClient,
    store: FileStateStore,
) -> OpsBot:
    # Replies ride the same delivery channel implementation as the gate
    # notifications, just bound to the ops chat.
    replier = TelegramNotifier(
        http=http,
        token=settings.TELEGRAM_API_TOKEN,
        chat_id=settings.TELEGRAM_LOG_CHAT_ID,
    )
    github = (
        GithubClient(
            http=http,
            token=settings.GITHUB_TOKEN,
            repo=settings.GITHUB_REPO,
        )
        if settings.GITHUB_TOKEN
        else None
    )
    return OpsBot(
        http=http,
        token=settings.TELEGRAM_API_TOKEN,
        chat_id=settings.TELEGRAM_LOG_CHAT_ID,
        watcher=watcher,
        client=client,
        store=store,
        replier=replier,
        tz=timezone(timedelta(hours=settings.TZ)),
        version=service_version(),
        github=github,
    )


# uv treats this project as virtual (no [build-system] in pyproject.toml),
# so no palgate-tg-notify distribution is ever installed and importlib
# metadata alone cannot resolve the version. Fall back to pyproject.toml:
# the Dockerfile flattens it next to main.py, in the repo it is one level up.
_PYPROJECT_PATHS = (
    Path(__file__).with_name("pyproject.toml"),
    Path(__file__).parents[1] / "pyproject.toml",
)


def service_version() -> str:
    try:
        return version("palgate-tg-notify")
    except PackageNotFoundError:
        pass
    for pyproject in _PYPROJECT_PATHS:
        try:
            with pyproject.open("rb") as file:
                found = toml_load(file)["project"]["version"]
        except (OSError, TOMLDecodeError, KeyError):
            continue
        if isinstance(found, str):
            return found
    return "unknown"


def read_stored_version(path: Path) -> str | None:
    try:
        stored = path.read_text().strip()
    except OSError:
        return None
    return stored or None


def store_version(path: Path, current: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(current + "\n")


def version_transition(previous: str | None, current: str) -> str | None:
    """Human-readable update/rollback notice, or None when nothing changed.

    The version file lives on the data volume, so the comparison survives
    container swaps — this is what turns a redeploy into an "Updated"
    notice and a rollback.yml run into a "Rolled back" one.
    """
    if previous is None or previous == current:
        return None
    try:
        previous_key = tuple(int(part) for part in previous.split("."))
        current_key = tuple(int(part) for part in current.split("."))
    except ValueError:
        return "Updated %s → %s" % (previous, current)
    if previous_key > current_key:
        return "Rolled back %s → %s" % (previous, current)
    return "Updated %s → %s" % (previous, current)


async def main() -> None:
    settings = Settings()

    configure_logging(settings)
    tz = timezone(timedelta(hours=settings.TZ))
    Formatter.converter = lambda *args: datetime.now(tz).timetuple()
    # Lifecycle events go to the "log" logger — i.e. the ops Telegram chat.
    log = getLogger("log")

    stop = Event()
    loop = get_running_loop()

    def request_stop(sig: Signals) -> None:
        log.info("Received %s, shutting down" % sig.name)
        stop.set()

    for sig in (SIGINT, SIGTERM):
        loop.add_signal_handler(sig, request_stop, sig)

    try:
        # Single-writer guarantee: wait out a previous container still
        # holding the state (e.g. the old instance during a deploy swap).
        store = FileStateStore(Path(settings.STATE_FILE))
        store.acquire_lock(settings.LOCK_TIMEOUT)
        try:
            async with AsyncClient() as http:
                client = build_client(settings, http)
                enrichment = build_enrichment(settings)
                enricher = None
                adapter = None
                if enrichment is not None:
                    enricher, adapter = enrichment
                    if not await adapter.connect():
                        # An unauthorized/broken session must not stop the
                        # service — run without enrichment.
                        enricher = None
                        adapter = None
                watcher = build_watcher(settings, http, store, client, enricher)
                bot = build_bot(settings, http, watcher, client, store)
                current_version = service_version()
                log.info(
                    "Started palgate-tg-notify %s, watching %s"
                    % (current_version, settings.DEVICE_ID)
                )
                if enricher is not None:
                    log.info("Telegram identity enrichment enabled")
                version_path = Path(settings.VERSION_FILE)
                notice = version_transition(
                    read_stored_version(version_path), current_version
                )
                if notice is not None:
                    log.info(notice)
                # Persist after announcing: a crash in between repeats the
                # notice on the next boot instead of losing it.
                store_version(version_path, current_version)
                tasks = [watcher.run(stop), bot.run(stop)]
                if enricher is not None:
                    tasks.append(enricher.run(stop))
                try:
                    await gather(*tasks)
                finally:
                    if adapter is not None:
                        await adapter.disconnect()
        finally:
            store.release_lock()
        log.info("Shut down cleanly")
    except Exception:
        log.exception("Service crashed")
        raise
    finally:
        # Drain queued ops-chat messages while the loop is still alive;
        # past this point only aiologging's 2s atexit fallback remains.
        await aio_shutdown(timeout=10.0)


if __name__ == "__main__":
    asyncio_run(main())
