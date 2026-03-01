from asyncio import to_thread as asyncio_to_thread
from enum import Enum
from logging import getLogger
from typing import Any

from pydantic import ValidationError
from requests import Response, HTTPError, ReadTimeout, request
from requests.exceptions import JSONDecodeError
from retry.api import retry_call

from constants import X_BT_TOKEN_HEADER
from models import ItemResponse


class Method(Enum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"
    PATCH = "PATCH"
    OPTIONS = "OPTIONS"


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
