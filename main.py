from asyncio import run as asyncio_run, gather as asyncio_gather, sleep as asyncio_sleep
from datetime import datetime, timezone, timedelta
from logging import Logger, getLogger, Formatter
from logging.config import dictConfig

from aiocache import BaseCache, SimpleMemoryCache
from pydantic import ValidationError
from pydantic_settings import BaseSettings
from pylgate import generate_token  # type: ignore[attr-defined]
from pylgate.types import TokenType
from requests import Response, HTTPError, get as requests_get
from requests.exceptions import JSONDecodeError
from retry.api import retry_call

from models import LogItem, LogItemResponse


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


class HttpClient:
    def __init__(
        self, timeout: float = 5, tries: int = 3, delay: float = 1, backoff: int = 2
    ) -> None:
        self.__timeout = timeout
        self.__tries = tries
        self.__delay = delay
        self.__backoff = backoff

    def __get(self, url: str, headers: dict[str, str]) -> Response:
        response = requests_get(url, headers=headers, timeout=self.__timeout)
        response.raise_for_status()
        return response

    def get(self, url: str, headers: dict[str, str]) -> Response:
        return retry_call(
            self.__get,
            (url, headers),
            exceptions=HTTPError,
            tries=self.__tries,
            delay=self.__delay,
            backoff=self.__backoff,
            logger=None,
        )


class LogUpdater:
    def __init__(
        self, settings: Settings, chat: Logger, log: Logger, cache: BaseCache
    ) -> None:
        self.__http_client = HttpClient()

        self.__url = settings.URL_USER_LOG.format(device_id=settings.DEVICE_ID)
        self.__headers = {"User-Agent": "okhttp/4.9.3"}

        self.__cron_delay = settings.CRON_DELAY

        self.__session_token = bytes.fromhex(settings.SESSION_TOKEN)
        self.__user_id = settings.USER_ID
        self.__session_token_type = settings.SESSION_TOKEN_TYPE

        self.__chat = chat
        self.__log = log
        self.__cache = cache

    @property
    def cron_delay(self) -> int:
        return self.__cron_delay

    def get_token(self) -> str:
        return generate_token(
            self.__session_token,
            self.__user_id,
            self.__session_token_type,
        )

    async def get_last_log_item(self) -> LogItem | None:
        log_item: LogItem | None = await self.__cache.get("last_log_item", None)
        return log_item

    async def set_last_log_item(self, item: LogItem) -> None:
        await self.__cache.add("last_log_item", item)

    def get_items(self) -> LogItemResponse | None:
        self.__headers["X-Bt-Token"] = self.get_token()

        try:
            response = self.__http_client.get(self.__url, self.__headers)
            print(response.json())
            return LogItemResponse.model_validate(response.json())
        except HTTPError as err:
            self.__log.error("HTTP failed: %s" % err)
            return None
        except JSONDecodeError as json_de:
            self.__log.error("JSON decode error: %s" % json_de)
            return None
        except ValidationError as ve:
            self.__log.error("Model validation error: %s" % ve)
            return None

    async def update_new_items(self) -> None:
        response = self.get_items()
        if response is None:
            return

        first_log_item = response.log[0]

        last_log_item = await self.get_last_log_item()
        if last_log_item is None:
            self.__log.debug("Set last log item:\n%s" % str(first_log_item))
            await self.set_last_log_item(first_log_item)
            return

        new_log_items: list[LogItem] = list()
        for log_item in response.log:
            if log_item != last_log_item:
                new_log_items.append(log_item)
            else:
                break

        if len(new_log_items) > 0:
            self.__chat.info("\n".join(str(log_item for log_item in new_log_items)))
            await self.set_last_log_item(first_log_item)


async def mainloop(updater: LogUpdater) -> None:
    while True:
        await updater.update_new_items()
        await asyncio_sleep(updater.cron_delay)


async def main() -> None:
    settings = Settings()

    dictConfig(
        {
            "version": 1,
            "formatters": {
                "chat": {
                    "class": "telegram_handler.HtmlFormatter",
                    "fmt": "%(message)s",
                }
            },
            "handlers": {
                "log": {
                    "class": "telegram_handler.TelegramHandler",
                    "token": settings.TELEGRAM_API_TOKEN,
                    "chat_id": settings.TELEGRAM_LOG_CHAT_ID,
                },
                "chat": {
                    "class": "telegram_handler.TelegramHandler",
                    "formatter": "chat",
                    "token": settings.TELEGRAM_API_TOKEN,
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                },
            },
            "loggers": {
                "log": {"handlers": ["log"], "level": "DEBUG"},
                "chat": {"handlers": ["chat"], "level": "INFO"},
            },
        }
    )

    tz = timezone(timedelta(hours=settings.TZ))
    Formatter.converter = lambda *args: datetime.now(tz).timetuple()

    client = LogUpdater(
        settings, getLogger("chat"), getLogger("log"), SimpleMemoryCache()
    )

    await asyncio_gather(mainloop(client))


if __name__ == "__main__":
    asyncio_run(main())
