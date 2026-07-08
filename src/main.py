from asyncio import Event, get_running_loop, run as asyncio_run
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from logging import Formatter, getLogger
from logging.config import dictConfig
from pathlib import Path
from signal import SIGINT, SIGTERM, Signals
from typing import Any

from httpx import AsyncClient

from config import Settings
from notify import TelegramNotifier
from palgate import PalgateClient
from service import GateWatcher
from state import FileStateStore


def build_logging_config(settings: Settings) -> dict[str, Any]:
    return {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(levelname)s][%(asctime)s] %(name)s: %(message)s",
            },
        },
        "handlers": {
            "log": {
                "class": "telegram_handler.TelegramHandler",
                "token": settings.TELEGRAM_API_TOKEN,
                "chat_id": settings.TELEGRAM_LOG_CHAT_ID,
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


def build_watcher(
    settings: Settings, http: AsyncClient, store: FileStateStore
) -> GateWatcher:
    client = PalgateClient(
        http=http,
        url=settings.URL_USER_LOG.format(device_id=settings.DEVICE_ID),
        session_token=settings.session_token_bytes,
        user_id=settings.USER_ID,
        token_type=settings.SESSION_TOKEN_TYPE,
    )
    notifier = TelegramNotifier(
        http=http,
        token=settings.TELEGRAM_API_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
    )
    return GateWatcher(
        source=settings.DEVICE_ID,
        client=client,
        store=store,
        notifiers=(notifier,),
        cron_delay=settings.CRON_DELAY,
        max_backoff=settings.MAX_BACKOFF,
        alert_after=settings.ALERT_AFTER_FAILURES,
        heartbeat_path=Path(settings.HEARTBEAT_FILE),
    )


def service_version() -> str:
    try:
        return version("palgate-tg-notify")
    except PackageNotFoundError:
        return "unknown"


async def main() -> None:
    settings = Settings()

    dictConfig(build_logging_config(settings))
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
                watcher = build_watcher(settings, http, store)
                log.info(
                    "Started palgate-tg-notify %s, watching %s"
                    % (service_version(), settings.DEVICE_ID)
                )
                await watcher.run(stop)
        finally:
            store.release_lock()
    except Exception:
        log.exception("Service crashed")
        raise
    log.info("Shut down cleanly")


if __name__ == "__main__":
    asyncio_run(main())
