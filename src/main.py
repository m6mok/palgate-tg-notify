from argparse import ArgumentParser, Namespace
from asyncio import (
    run as asyncio_run,
    gather as asyncio_gather,
    sleep as asyncio_sleep,
)
from datetime import datetime, timezone, timedelta
from logging import Formatter, getLogger
from logging.config import dictConfig

from aiocache import SimpleMemoryCache

from config import Environment, Settings
from handlers import BroadcastLoggerHandler, LogItemCacheHandler
from services import LogUpdater


def get_args() -> Namespace:
    parser = ArgumentParser()
    parser.add_argument(
        "--env",
        type=Environment,
        default=Environment.DEV,
        help="Environment [DEV, STABLE]",
    )
    parser.add_argument(
        "--dev-url",
        type=str,
        default="localhost:8080/api/log",
        help="PalGate history getter url",
    )
    return parser.parse_args()


async def mainloop(updater: LogUpdater) -> None:
    while True:
        await updater.update_new_items_save()
        await asyncio_sleep(updater.cron_delay)


async def main() -> None:
    args = get_args()

    settings = Settings()
    settings.ENVIRONMENT = args.env

    if settings.ENVIRONMENT == Environment.DEV:
        settings.URL_USER_LOG = args.dev_url

    dictConfig(
        {
            "version": 1,
            "formatters": {
                "chat": {
                    "class": "formatter.HtmlFormatter",
                    "format": "%(message)s",
                },
                "default": {
                    "format": (
                        "[%(levelname)s][%(asctime)s] %(name)s: %(message)s"
                    ),
                },
            },
            "handlers": {
                "log": {
                    "class": "telegram_handler.TelegramHandler",
                    "token": settings.TELEGRAM_API_TOKEN,
                    "chat_id": settings.TELEGRAM_LOG_CHAT_ID,
                },
                "tg_chat": {
                    "class": "telegram_handler.TelegramHandler",
                    "formatter": "chat",
                    "token": settings.TELEGRAM_API_TOKEN,
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                },
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                },
                "file": {
                    "class": "logging.FileHandler",
                    "filename": "palgate.log",
                    "formatter": "default",
                },
            },
            "loggers": {
                "default": {"handlers": ["stdout", "file"], "level": "DEBUG"},
                "log": {
                    "handlers": ["log", "stdout", "file"],
                    "level": "DEBUG",
                },
                "chat": {
                    "handlers": ["tg_chat", "stdout", "file"],
                    "level": "INFO",
                },
            },
        }
    )

    tz = timezone(timedelta(hours=settings.TZ))
    Formatter.converter = lambda *args: datetime.now(tz).timetuple()

    client = LogUpdater(
        settings,
        BroadcastLoggerHandler(
            [getLogger("tg_chat")],
        ),
        LogItemCacheHandler(SimpleMemoryCache()),
    )

    await asyncio_gather(mainloop(client))


if __name__ == "__main__":
    asyncio_run(main())
