from argparse import ArgumentParser, Namespace
from asyncio import (
    run as asyncio_run,
    gather as asyncio_gather,
    sleep as asyncio_sleep,
    to_thread as asyncio_to_thread,
)
from datetime import datetime, timezone, timedelta
from enum import Enum
from itertools import takewhile
from logging import Logger, getLogger, Formatter
from logging.config import dictConfig
from typing import Any, Iterable, cast

from aiocache import BaseCache, SimpleMemoryCache
from pydantic import ValidationError
from pydantic_settings import BaseSettings
from pylgate.token_generator import generate_token
from pylgate.types import TokenType
from requests import Response, HTTPError, ReadTimeout, request
from requests.exceptions import JSONDecodeError
from retry.api import retry_call

from models import LogItem, Item, ItemResponse


X_BT_TOKEN_HEADER = "X-Bt-Token"


class Method(Enum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"
    PATCH = "PATCH"
    OPTIONS = "OPTIONS"


class Environment(Enum):
    DEV = "dev"
    STABLE = "stable"


class Settings(BaseSettings):
    DEVICE_ID: str
    USER_ID: int
    SESSION_TOKEN: str
    SESSION_TOKEN_TYPE: TokenType
    URL_USER_LOG: str
    TZ: int
    TELEGRAM_API_TOKEN: str
    TELEGRAM_CHAT_ID: int
    TELEGRAM_LOG_CHAT_ID: int
    CRON_DELAY: int
    ENVIRONMENT: Environment = Environment.DEV


class HttpHandlerBase:
    def __init__(
        self,
        path: str,
        method: Method | None = None,
        timeout: float | None = None,
        tries: int | None = None,
        delay: float | None = None,
        backoff: int | None = None,
    ) -> None:
        self._path = path

        if method is None:
            method = Method.GET
        self._method = method

        if timeout is None:
            timeout = 1  # sec
        self._timeout = timeout

        if tries is None:
            tries = 0  # without tries by default
        self._tries = tries

        if delay is None:
            delay = 0
        self._delay = delay

        if backoff is None:
            backoff = 0
        self._backoff = backoff

        self._log = getLogger("log")

    async def request(
        self,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        raise NotImplementedError

    async def __call__(
        self,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        raise NotImplementedError


class SyncHttpHandler(HttpHandlerBase):
    def __init__(
        self,
        path: str,
        method: Method | None = None,
        timeout: float | None = None,
        tries: int | None = None,
        delay: float | None = None,
        backoff: int | None = None,
    ) -> None:
        super().__init__(path, method, timeout, tries, delay, backoff)

    def __request(
        self,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        response = request(
            self._method.value,
            self._path,
            params=params,
            headers=headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response

    async def request(
        self,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return await asyncio_to_thread(
            retry_call,
            self.__request,
            (params, headers),
            exceptions=(HTTPError, ReadTimeout),
            tries=self._tries,
            delay=self._delay,
            backoff=self._backoff,
            logger=self._log,
        )

    async def __call__(
        self,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return await self.request(params, headers)


class PalGateItemsHandler(SyncHttpHandler):
    def __init__(
        self,
        path: str,
        method: Method = Method.GET,
        timeout: float = 5,
        tries: int = 3,
        delay: float = 1,
        backoff: int = 2,
    ) -> None:
        super().__init__(path, method, timeout, tries, delay, backoff)

    async def __call__(
        self,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> ItemResponse:
        if params is None:
            params = dict()

        if headers is None:
            headers = dict()

        if X_BT_TOKEN_HEADER not in headers:
            self._log.warning("No X_BT_TOKEN_HEADER")

        try:
            response = await self.request(params, headers)
            return ItemResponse.model_validate(response.json())
        except HTTPError as err:
            self._log.error("HTTP failed: %s" % err)
            raise err
        except JSONDecodeError as json_de:
            self._log.error("JSON decode error: %s" % json_de)
            raise json_de
        except ValidationError as ve:
            self._log.error("Model validation error: %s" % ve)
            raise ve


class PalGateTokenGenerator:
    def __init__(
        self,
        session_token: bytes,
        user_id: int,
        session_token_type: TokenType,
    ) -> None:
        self.__session_token = session_token
        self.__user_id = user_id
        self.__session_token_type = session_token_type

    async def __call__(self) -> str:
        await asyncio_sleep(0)
        return generate_token(
            self.__session_token,
            self.__user_id,
            self.__session_token_type,
        )


class CacheHandlerBase:
    def __init__(self) -> None:
        self._log = getLogger("log")

    async def get(
        self, key: str | None = None, default: Any | None = None
    ) -> Any:
        raise NotImplementedError

    async def set(
        self, key: str | None = None, value: Any | None = None
    ) -> None:
        raise NotImplementedError

    async def add(
        self, key: str | None = None, value: Any | None = None
    ) -> None:
        raise NotImplementedError


class CacheHandler(CacheHandlerBase):
    def __init__(self, cache: BaseCache) -> None:
        super().__init__()

        self.__cache = cache

    async def get(
        self, key: str | None = None, default: Any | None = None
    ) -> Any:
        if key is None:
            self._log.warning("Key is None")
        return await self.__cache.get(key, default=default)

    async def set(
        self, key: str | None = None, value: Any | None = None
    ) -> None:
        if key is None:
            self._log.warning("Key is None")
        await self.__cache.set(key, value)

    async def add(
        self, key: str | None = None, value: Any | None = None
    ) -> None:
        if key is None:
            self._log.warning("Key is None")
        await self.__cache.add(key, value)


class LogItemCacheHandler(CacheHandlerBase):
    def __init__(
        self,
        cache: BaseCache,
        key: str | None = None,
    ) -> None:
        super().__init__()

        self.__cache = cache

        if key is None:
            key = "last_log_item"
        self.__key = key

    async def get(
        self, key: str | None = None, default: Any | None = None
    ) -> LogItem | None:
        if key is None:
            key = self.__key
        return cast(
            LogItem | None,
            await self.__cache.get(key, default=default),
        )

    async def set(
        self, key: str | None = None, value: LogItem | None = None
    ) -> None:
        if key is None:
            key = self.__key
        await self.__cache.set(key, value)

    async def add(
        self, key: str | None = None, value: LogItem | None = None
    ) -> None:
        if key is None:
            key = self.__key
        await self.__cache.add(key, value)


class BroadcastHandlerBase:
    def __init__(self) -> None:
        self._log = getLogger("log")

    async def __call__(self, message: str | None = None) -> None:
        raise NotImplementedError


class BroadcastLoggerHandler(BroadcastHandlerBase):
    def __init__(self, loggers: Iterable[Logger]) -> None:
        super().__init__()

        self.__loggers = tuple(loggers)

    async def __call__(self, message: str | None = None) -> None:
        if message is None or len(message) == 0:
            return

        for logger in self.__loggers:
            logger.info(message)


class LogUpdater:
    def __init__(
        self,
        settings: Settings,
        broadcaster: BroadcastHandlerBase,
        log_item_cache: LogItemCacheHandler,
    ) -> None:
        self.__items_handler = PalGateItemsHandler(settings.URL_USER_LOG)

        self.__params = {"id": settings.DEVICE_ID}
        self.__headers = {"User-Agent": "okhttp/4.9.3"}

        self.__token_generator = PalGateTokenGenerator(
            bytes.fromhex(settings.SESSION_TOKEN),
            settings.USER_ID,
            settings.SESSION_TOKEN_TYPE,
        )

        self.__cron_delay = settings.CRON_DELAY

        self.__broadcaster = broadcaster

        self._log = getLogger("log")

        self.__log_item_cache = log_item_cache

    @property
    def cron_delay(self) -> int:
        return self.__cron_delay

    async def update_new_items_save(self) -> None:
        try:
            await self.__update_new_items()
        except Exception:
            pass

    async def __update_new_items(self) -> None:
        self.__headers[X_BT_TOKEN_HEADER] = await self.__token_generator()
        response = await self.__items_handler(
            params=self.__params, headers=self.__headers
        )
        if response.log is None or len(response.log) == 0:
            raise ValueError("Wrong log list: %s" % str(response))

        first_log_item = response.log[0]

        last_log_item = await self.__log_item_cache.get()
        if last_log_item is None:
            self._log.debug(
                "Last log item: %s" % repr(Item.from_log_item(first_log_item))
            )
            await self.__log_item_cache.add(value=first_log_item)
            return

        new_log_items = takewhile(
            lambda item: item != last_log_item, response.log
        )
        messages = tuple(
            str(Item.from_log_item(log_item)) for log_item in new_log_items
        )
        message = "\n".join(reversed(messages))

        if message != "":
            await self.__broadcaster(message)
            await self.__log_item_cache.set(value=first_log_item)


async def mainloop(updater: LogUpdater) -> None:
    while True:
        await updater.update_new_items_save()
        await asyncio_sleep(updater.cron_delay)


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
