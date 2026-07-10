from asyncio import sleep as asyncio_sleep
from json import JSONDecodeError
from logging import getLogger
from typing import Any, Protocol

from httpx import AsyncClient, Response, TransportError


class NotifyError(Exception):
    """Delivery failed.

    ``permanent`` distinguishes messages the channel will never accept (e.g.
    a 400 from the API) from outages worth retrying on the next poll cycle.
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class Notifier(Protocol):
    """A delivery channel; ``send`` returns only on confirmed delivery.

    ``send`` returns the message id when the channel can later edit that
    message (used to append resolved data), or ``None`` when it cannot — the
    enrichment step is skipped for such channels. ``edit`` is only ever called
    with an id a previous ``send`` handed back.
    """

    @property
    def name(self) -> str: ...

    async def send(self, text: str) -> int | None: ...

    async def edit(self, message_id: int, text: str) -> None: ...


class TelegramNotifier:
    """Delivers messages through the Telegram Bot API.

    Retries transport failures and 5xx with exponential backoff, honours the
    ``retry_after`` hint on 429, and raises a permanent ``NotifyError`` on
    other 4xx so the caller can skip a message Telegram will never accept.
    """

    def __init__(
        self,
        http: AsyncClient,
        token: str,
        chat_id: int,
        timeout: float = 5,
        tries: int = 3,
        delay: float = 1,
    ) -> None:
        self._http = http
        self._base = "https://api.telegram.org/bot%s/" % token
        self._chat_id = chat_id
        self._timeout = timeout
        self._tries = tries
        self._delay = delay
        self._log = getLogger("default")

    @property
    def name(self) -> str:
        return "telegram"

    async def send(self, text: str) -> int | None:
        response = await self._call(
            "sendMessage",
            {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
        )
        return self._message_id(response)

    async def edit(self, message_id: int, text: str) -> None:
        await self._call(
            "editMessageText",
            {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            },
        )

    async def _call(self, method: str, payload: dict[str, Any]) -> Response:
        url = self._base + method
        last_error = "no attempts made"
        delay = self._delay
        for attempt in range(1, self._tries + 1):
            try:
                response = await self._http.post(
                    url, json=payload, timeout=self._timeout
                )
            except TransportError as err:
                last_error = "transport failed: %s" % err
            else:
                if response.status_code == 200:
                    return response
                if response.status_code == 429:
                    delay = max(delay, self._retry_after(response))
                    last_error = "rate limited (429)"
                elif response.status_code >= 500:
                    last_error = "telegram responded %d" % response.status_code
                else:
                    raise NotifyError(
                        "Telegram rejected %s: %d %s"
                        % (method, response.status_code, response.text),
                        permanent=True,
                    )
            if attempt < self._tries:
                self._log.warning(
                    "Telegram %s attempt %d/%d failed (%s), "
                    "retrying in %.1fs"
                    % (method, attempt, self._tries, last_error, delay)
                )
                await asyncio_sleep(delay)
                delay *= 2
        raise NotifyError(
            "Telegram unreachable after %d tries: %s"
            % (self._tries, last_error)
        )

    @staticmethod
    def _message_id(response: Response) -> int | None:
        try:
            message_id = response.json()["result"]["message_id"]
        except (JSONDecodeError, KeyError, TypeError):
            return None
        return message_id if isinstance(message_id, int) else None

    def _retry_after(self, response: Response) -> float:
        try:
            retry_after = response.json()["parameters"]["retry_after"]
        except (JSONDecodeError, KeyError, TypeError):
            return self._delay
        if isinstance(retry_after, (int, float)):
            return float(retry_after)
        return self._delay


class MaxNotifier:
    """Delivers messages through the Max messenger Bot API.

    Same delivery contract as ``TelegramNotifier``: transport failures,
    5xx and 429 are retried with exponential backoff; any other 4xx raises
    a permanent ``NotifyError``. Max authenticates with the token as a
    query parameter, not a header.
    """

    def __init__(
        self,
        http: AsyncClient,
        token: str,
        chat_id: int,
        timeout: float = 5,
        tries: int = 3,
        delay: float = 1,
    ) -> None:
        self._http = http
        self._url = "https://botapi.max.ru/messages"
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._tries = tries
        self._delay = delay
        self._log = getLogger("default")

    @property
    def name(self) -> str:
        return "max"

    async def edit(self, message_id: int, text: str) -> None:
        # Enrichment targets the Telegram channel; ``send`` returns None here
        # so the enricher never asks Max to edit. Kept for protocol parity.
        raise NotifyError("Max does not support editing", permanent=True)

    async def send(self, text: str) -> int | None:
        params: dict[str, Any] = {
            "access_token": self._token,
            "chat_id": self._chat_id,
        }
        payload: dict[str, Any] = {"text": text, "format": "html"}
        last_error = "no attempts made"
        delay = self._delay
        for attempt in range(1, self._tries + 1):
            try:
                response = await self._http.post(
                    self._url, params=params, json=payload, timeout=self._timeout
                )
            except TransportError as err:
                last_error = "transport failed: %s" % err
            else:
                if response.status_code == 200:
                    return None
                if response.status_code == 429:
                    last_error = "rate limited (429)"
                elif response.status_code >= 500:
                    last_error = "max responded %d" % response.status_code
                else:
                    raise NotifyError(
                        "Max rejected the message: %d %s"
                        % (response.status_code, response.text),
                        permanent=True,
                    )
            if attempt < self._tries:
                self._log.warning(
                    "Max send attempt %d/%d failed (%s), retrying in %.1fs"
                    % (attempt, self._tries, last_error, delay)
                )
                await asyncio_sleep(delay)
                delay *= 2
        raise NotifyError(
            "Max unreachable after %d tries: %s" % (self._tries, last_error)
        )
