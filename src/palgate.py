from json import JSONDecodeError
from logging import WARNING, getLogger

from httpx import AsyncClient, Response, TransportError
from pydantic import ValidationError
from pylgate.token_generator import generate_token
from pylgate.types import TokenType
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from models import ItemResponse


class PalgateError(Exception):
    """Base for everything that can go wrong while fetching the gate log."""


class TransientFetchError(PalgateError):
    """Network failures and 5xx/429 responses — safe to retry."""


class AuthError(PalgateError):
    """4xx responses — the API rejects the request itself; retrying won't help."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidResponseError(PalgateError):
    """A 2xx response with a body that cannot be parsed or validated."""


class PalgateClient:
    """Asynchronous client for the Palgate user access log endpoint.

    Every attempt generates a fresh X-Bt-Token: pylgate tokens embed the
    current timestamp and are only valid for a few seconds, so a token must
    never be reused across retries.
    """

    def __init__(
        self,
        http: AsyncClient,
        url: str,
        session_token: bytes,
        user_id: int,
        token_type: TokenType,
        timeout: float = 5,
        tries: int = 3,
        delay: float = 1,
    ) -> None:
        self._http = http
        self._url = url
        self._session_token = session_token
        self._user_id = user_id
        self._token_type = token_type
        self._timeout = timeout
        self._tries = tries
        self._delay = delay
        self._log = getLogger("default")

    def generate_token(self) -> str:
        return generate_token(self._session_token, self._user_id, self._token_type)

    async def fetch_log(self) -> ItemResponse:
        try:
            response = await self._get_with_retries()
        except TransportError as err:
            raise TransientFetchError("HTTP transport failed: %s" % err) from err

        try:
            payload = response.json()
        except JSONDecodeError as err:
            raise InvalidResponseError("JSON decode error: %s" % err) from err

        try:
            return ItemResponse.model_validate(payload)
        except ValidationError as err:
            raise InvalidResponseError("Model validation error: %s" % err) from err

    async def _get_with_retries(self) -> Response:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._tries),
            wait=wait_exponential(multiplier=self._delay),
            retry=retry_if_exception_type((TransientFetchError, TransportError)),
            before_sleep=before_sleep_log(self._log, WARNING),
            reraise=True,
        )
        response: Response = await retrying(self._get)
        return response

    async def _get(self) -> Response:
        headers = {"User-Agent": "okhttp/4.9.3", "X-Bt-Token": self.generate_token()}
        response = await self._http.get(self._url, headers=headers, timeout=self._timeout)
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientFetchError(
                "Palgate API responded %d" % response.status_code
            )
        if response.status_code >= 400:
            raise AuthError(
                "Palgate API rejected the request: %d" % response.status_code,
                status_code=response.status_code,
            )
        return response
