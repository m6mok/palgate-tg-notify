from itertools import takewhile
from logging import getLogger

from config import Settings
from handlers import (
    BroadcastHandlerBase,
    LogItemCacheHandler,
    PalGateItemsHandler,
)
from services.token_generator import PalGateTokenGenerator

from constants import X_BT_TOKEN_HEADER
from models import Item


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
