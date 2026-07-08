from json import loads as json_loads
from typing import Callable, List, Tuple

import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Request, Response

from notify import NotifyError, TelegramNotifier


Handler = Callable[[Request], Response]


def make_notifier(
    handler: Handler, tries: int = 3
) -> Tuple[TelegramNotifier, List[Request]]:
    seen: List[Request] = []

    def recording_handler(request: Request) -> Response:
        seen.append(request)
        return handler(request)

    http = AsyncClient(transport=MockTransport(recording_handler))
    notifier = TelegramNotifier(
        http=http,
        token="test:token",
        chat_id=42,
        tries=tries,
        delay=0,
    )
    return notifier, seen


class TestSend:
    @pytest.mark.asyncio
    async def test_sends_html_message_to_the_bot_api(self) -> None:
        notifier, seen = make_notifier(lambda _: Response(200, json={"ok": True}))

        await notifier.send("hello <b>world</b>")

        request = seen[0]
        assert request.url.path == "/bottest:token/sendMessage"
        payload = json_loads(request.content)
        assert payload == {
            "chat_id": 42,
            "text": "hello <b>world</b>",
            "parse_mode": "HTML",
        }

    def test_channel_name_is_telegram(self) -> None:
        notifier, _ = make_notifier(lambda _: Response(200))

        assert notifier.name == "telegram"

    @pytest.mark.asyncio
    async def test_5xx_is_retried_until_success(self) -> None:
        responses = [Response(500), Response(502), Response(200)]
        notifier, seen = make_notifier(lambda _: responses.pop(0), tries=3)

        await notifier.send("hi")

        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_persistent_outage_raises_retriable_error(self) -> None:
        notifier, seen = make_notifier(lambda _: Response(500), tries=2)

        with pytest.raises(NotifyError) as exc_info:
            await notifier.send("hi")

        assert exc_info.value.permanent is False
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_transport_failure_raises_retriable_error(self) -> None:
        def broken(_: Request) -> Response:
            raise ConnectError("connection refused")

        notifier, seen = make_notifier(broken, tries=2)

        with pytest.raises(NotifyError) as exc_info:
            await notifier.send("hi")

        assert exc_info.value.permanent is False
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_429_honours_retry_after_and_retries(self) -> None:
        responses = [
            Response(429, json={"ok": False, "parameters": {"retry_after": 0}}),
            Response(200),
        ]
        notifier, seen = make_notifier(lambda _: responses.pop(0), tries=2)

        await notifier.send("hi")

        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_429_without_body_still_retries(self) -> None:
        responses = [Response(429, content=b""), Response(200)]
        notifier, seen = make_notifier(lambda _: responses.pop(0), tries=2)

        await notifier.send("hi")

        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_4xx_is_permanent_and_not_retried(self) -> None:
        notifier, seen = make_notifier(
            lambda _: Response(400, json={"description": "bad request"}),
            tries=3,
        )

        with pytest.raises(NotifyError) as exc_info:
            await notifier.send("hi")

        assert exc_info.value.permanent is True
        assert len(seen) == 1
