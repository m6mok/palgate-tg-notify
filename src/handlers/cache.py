from logging import getLogger
from typing import Any, cast

from aiocache import BaseCache

from models import LogItem


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
