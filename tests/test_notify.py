from json import loads as json_loads
from typing import Callable, List, Tuple

import pytest
from httpx import AsyncClient, ConnectError, MockTransport, Request, Response

from notify import MaxNotifier, NotifyError, TelegramNotifier


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


def make_max_notifier(
    handler: Handler, tries: int = 3
) -> Tuple[MaxNotifier, List[Request]]:
    seen: List[Request] = []

    def recording_handler(request: Request) -> Response:
        seen.append(request)
        return handler(request)

    http = AsyncClient(transport=MockTransport(recording_handler))
    notifier = MaxNotifier(
        http=http,
        token="max_token",
        chat_id=77,
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

    @pytest.mark.asyncio
    async def test_send_returns_message_id(self) -> None:
        notifier, _ = make_notifier(
            lambda _: Response(200, json={"ok": True, "result": {"message_id": 555}})
        )

        assert await notifier.send("hi") == 555

    @pytest.mark.asyncio
    async def test_send_returns_none_when_id_absent(self) -> None:
        notifier, _ = make_notifier(lambda _: Response(200, json={"ok": True}))

        assert await notifier.send("hi") is None


class TestEdit:
    @pytest.mark.asyncio
    async def test_edits_message_via_edit_message_text(self) -> None:
        notifier, seen = make_notifier(lambda _: Response(200, json={"ok": True}))

        await notifier.edit(555, "hi <b>enriched</b>")

        request = seen[0]
        assert request.url.path == "/bottest:token/editMessageText"
        assert json_loads(request.content) == {
            "chat_id": 42,
            "message_id": 555,
            "text": "hi <b>enriched</b>",
            "parse_mode": "HTML",
        }

    @pytest.mark.asyncio
    async def test_edit_retries_5xx(self) -> None:
        responses = [Response(500), Response(200, json={"ok": True})]
        notifier, seen = make_notifier(lambda _: responses.pop(0), tries=2)

        await notifier.edit(1, "x")

        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_edit_4xx_is_permanent(self) -> None:
        notifier, seen = make_notifier(
            lambda _: Response(400, json={"description": "message not found"}),
            tries=3,
        )

        with pytest.raises(NotifyError) as exc_info:
            await notifier.edit(1, "x")

        assert exc_info.value.permanent is True
        assert len(seen) == 1


class TestMaxSend:
    @pytest.mark.asyncio
    async def test_sends_html_message_with_query_auth(self) -> None:
        notifier, seen = make_max_notifier(lambda _: Response(200, json={}))

        await notifier.send("hello <b>world</b>")

        request = seen[0]
        assert request.url.host == "botapi.max.ru"
        assert request.url.path == "/messages"
        assert request.url.params["access_token"] == "max_token"
        assert request.url.params["chat_id"] == "77"
        payload = json_loads(request.content)
        assert payload == {"text": "hello <b>world</b>", "format": "html"}

    def test_channel_name_is_max(self) -> None:
        notifier, _ = make_max_notifier(lambda _: Response(200))

        assert notifier.name == "max"

    @pytest.mark.asyncio
    async def test_send_returns_none_no_editable_id(self) -> None:
        notifier, _ = make_max_notifier(lambda _: Response(200, json={}))

        assert await notifier.send("hi") is None

    @pytest.mark.asyncio
    async def test_edit_is_unsupported(self) -> None:
        notifier, seen = make_max_notifier(lambda _: Response(200))

        with pytest.raises(NotifyError) as exc_info:
            await notifier.edit(1, "x")

        assert exc_info.value.permanent is True
        assert seen == []  # no HTTP call made

    @pytest.mark.asyncio
    async def test_5xx_is_retried_until_success(self) -> None:
        responses = [Response(500), Response(502), Response(200)]
        notifier, seen = make_max_notifier(lambda _: responses.pop(0), tries=3)

        await notifier.send("hi")

        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_persistent_outage_raises_retriable_error(self) -> None:
        notifier, seen = make_max_notifier(lambda _: Response(500), tries=2)

        with pytest.raises(NotifyError) as exc_info:
            await notifier.send("hi")

        assert exc_info.value.permanent is False
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_transport_failure_raises_retriable_error(self) -> None:
        def broken(_: Request) -> Response:
            raise ConnectError("connection refused")

        notifier, seen = make_max_notifier(broken, tries=2)

        with pytest.raises(NotifyError) as exc_info:
            await notifier.send("hi")

        assert exc_info.value.permanent is False
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_429_is_retried(self) -> None:
        responses = [Response(429, json={}), Response(200)]
        notifier, seen = make_max_notifier(lambda _: responses.pop(0), tries=2)

        await notifier.send("hi")

        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_4xx_is_permanent_and_not_retried(self) -> None:
        notifier, seen = make_max_notifier(
            lambda _: Response(400, json={"code": "chat.denied"}),
            tries=3,
        )

        with pytest.raises(NotifyError) as exc_info:
            await notifier.send("hi")

        assert exc_info.value.permanent is True
        assert len(seen) == 1
