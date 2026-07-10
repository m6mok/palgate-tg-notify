from typing import Any

import pytest
from telethon.errors import FloodWaitError

from resolver import FloodError, Profile
from telegram_resolver import TelegramContactResolver


class FakeUser:
    def __init__(
        self, id: int, username: str | None, first_name: str, last_name: str
    ) -> None:
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class FakeResult:
    def __init__(self, users: list[Any]) -> None:
        self.users = users


class FakeClient:
    def __init__(
        self,
        result: FakeResult | None = None,
        flood_seconds: int | None = None,
        authorized: bool = True,
    ) -> None:
        self._result = result
        self._flood_seconds = flood_seconds
        self._authorized = authorized
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def disconnect(self) -> None:
        self.disconnected = True

    def __call__(self, request: Any) -> Any:
        return self._invoke()

    async def _invoke(self) -> Any:
        if self._flood_seconds is not None:
            err = FloodWaitError.__new__(FloodWaitError)
            err.seconds = self._flood_seconds
            raise err
        return self._result


def make(client: FakeClient) -> TelegramContactResolver:
    return TelegramContactResolver(client)  # type: ignore[arg-type]


class TestConnect:
    @pytest.mark.asyncio
    async def test_authorized_session_connects(self) -> None:
        resolver = make(FakeClient(authorized=True))
        assert await resolver.connect() is True

    @pytest.mark.asyncio
    async def test_unauthorized_session_disconnects_and_reports_false(self) -> None:
        client = FakeClient(authorized=False)
        resolver = make(client)
        assert await resolver.connect() is False
        assert client.disconnected is True

    @pytest.mark.asyncio
    async def test_disconnect_delegates(self) -> None:
        client = FakeClient()
        await make(client).disconnect()
        assert client.disconnected is True


class TestResolve:
    @pytest.mark.asyncio
    async def test_maps_user_to_profile(self) -> None:
        user = FakeUser(7, "neo", "Thomas", "Anderson")
        resolver = make(FakeClient(result=FakeResult([user])))

        profile = await resolver.resolve("79001234567")

        assert profile == Profile(
            user_id=7, username="neo", firstname="Thomas", lastname="Anderson"
        )

    @pytest.mark.asyncio
    async def test_no_users_means_absent(self) -> None:
        resolver = make(FakeClient(result=FakeResult([])))
        assert await resolver.resolve("79001234567") is None

    @pytest.mark.asyncio
    async def test_flood_wait_becomes_flood_error(self) -> None:
        resolver = make(FakeClient(flood_seconds=42))
        with pytest.raises(FloodError) as exc_info:
            await resolver.resolve("79001234567")
        assert exc_info.value.seconds == 42


class TestClientId:
    def test_is_deterministic_per_phone(self) -> None:
        a = TelegramContactResolver._client_id("79001234567")
        b = TelegramContactResolver._client_id("79001234567")
        c = TelegramContactResolver._client_id("79009876543")
        assert a == b
        assert a != c
