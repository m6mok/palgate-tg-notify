from logging import Logger, getLogger
from typing import Iterable


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
