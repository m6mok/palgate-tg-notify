from typing import Awaitable, Callable, List, Tuple

import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Request, Response
from pylgate.types import TokenType

from palgate import (
    AuthError,
    InvalidResponseError,
    PalgateClient,
    TransientFetchError,
)
from tests.conftest import (
    BASE_LOG_ITEM_DATA,
    SECOND_LOG_ITEM_DATA,
    SESSION_TOKEN_HEX,
)


Handler = Callable[[Request], Response]

VALID_PAYLOAD = {
    "log": [SECOND_LOG_ITEM_DATA, BASE_LOG_ITEM_DATA],
    "err": False,
    "msg": "Success",
    "status": "ok",
}


def make_client(
    handler: Handler, tries: int = 3
) -> Tuple[PalgateClient, List[Request]]:
    seen: List[Request] = []

    def recording_handler(request: Request) -> Response:
        seen.append(request)
        return handler(request)

    http = AsyncClient(transport=MockTransport(recording_handler))
    client = PalgateClient(
        http=http,
        url="https://api.test/log",
        session_token=bytes.fromhex(SESSION_TOKEN_HEX),
        user_id=12345,
        token_type=TokenType.SMS,
        tries=tries,
        delay=0,
    )
    return client, seen


class TestFetchLog:
    @pytest.mark.asyncio
    async def test_valid_response_is_parsed(self) -> None:
        client, seen = make_client(lambda _: Response(200, json=VALID_PAYLOAD))

        response = await client.fetch_log()

        assert response.status == "ok"
        assert response.log is not None
        assert len(response.log) == 2
        assert response.log[0].userId == "67890"
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_every_request_carries_auth_headers(self) -> None:
        client, seen = make_client(lambda _: Response(200, json=VALID_PAYLOAD))

        await client.fetch_log()

        request = seen[0]
        assert request.headers["User-Agent"] == "okhttp/4.9.3"
        token = request.headers["X-Bt-Token"]
        assert len(bytes.fromhex(token)) == 23

    @pytest.mark.asyncio
    async def test_invalid_json_raises_invalid_response(self) -> None:
        client, _ = make_client(lambda _: Response(200, content=b"not json"))

        with pytest.raises(InvalidResponseError, match="JSON decode error"):
            await client.fetch_log()

    @pytest.mark.asyncio
    async def test_unvalidatable_body_raises_invalid_response(self) -> None:
        payload = {"log": [], "err": False, "msg": "", "status": "ok"}
        client, _ = make_client(lambda _: Response(200, json=payload))

        with pytest.raises(InvalidResponseError, match="validation"):
            await client.fetch_log()


class TestRetries:
    @pytest.mark.asyncio
    async def test_transient_5xx_is_retried_until_success(self) -> None:
        responses = [Response(500), Response(502), Response(200, json=VALID_PAYLOAD)]
        client, seen = make_client(lambda _: responses.pop(0), tries=3)

        response = await client.fetch_log()

        assert response.status == "ok"
        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_persistent_5xx_raises_after_all_tries(self) -> None:
        client, seen = make_client(lambda _: Response(500), tries=2)

        with pytest.raises(TransientFetchError):
            await client.fetch_log()

        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_429_is_treated_as_transient(self) -> None:
        responses = [Response(429), Response(200, json=VALID_PAYLOAD)]
        client, seen = make_client(lambda _: responses.pop(0), tries=2)

        response = await client.fetch_log()

        assert response.status == "ok"
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_network_failure_is_retried_then_wrapped(self) -> None:
        def broken(_: Request) -> Response:
            raise ConnectError("connection refused")

        client, seen = make_client(broken, tries=3)

        with pytest.raises(TransientFetchError, match="transport failed"):
            await client.fetch_log()

        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_fresh_token_is_generated_for_each_attempt(self) -> None:
        responses = [Response(500), Response(200, json=VALID_PAYLOAD)]
        client, seen = make_client(lambda _: responses.pop(0), tries=2)

        await client.fetch_log()

        assert all("X-Bt-Token" in request.headers for request in seen)


class TestAuthErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 401, 402, 403, 404])
    async def test_4xx_raises_auth_error_without_retrying(
        self, status_code: int
    ) -> None:
        client, seen = make_client(lambda _: Response(status_code), tries=3)

        with pytest.raises(AuthError) as exc_info:
            await client.fetch_log()

        assert exc_info.value.status_code == status_code
        assert len(seen) == 1
