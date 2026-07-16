from typing import Any

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import (
    AddContactRequest,
    DeleteContactsRequest,
    ImportContactsRequest,
)

from resolver import FloodError, Profile
from telegram_resolver import TelegramContactResolver


class FakeUser:
    def __init__(
        self,
        id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class FakeResult:
    """Shape shared by the import and delete responses: a ``users`` list."""

    def __init__(self, users: list[Any]) -> None:
        self.users = users


class FakeClient:
    """Replays a response per request type and records every request.

    ``imported`` is what ``ImportContactsRequest`` returns (the user under
    the contact-list name we just set); ``deleted`` is what
    ``DeleteContactsRequest`` returns (the user under their own profile
    name). ``flood_on`` raises a FloodWait for that request type.
    """

    def __init__(
        self,
        imported: list[Any] | None = None,
        deleted: list[Any] | None = None,
        flood_on: type | None = None,
        flood_seconds: int = 42,
        authorized: bool = True,
    ) -> None:
        self._imported = imported if imported is not None else []
        self._deleted = (
            deleted if deleted is not None else list(self._imported)
        )
        self._flood_on = flood_on
        self._flood_seconds = flood_seconds
        self._authorized = authorized
        self.disconnected = False
        self.requests: list[Any] = []

    async def connect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def disconnect(self) -> None:
        self.disconnected = True

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self._invoke(request)

    async def _invoke(self, request: Any) -> Any:
        if self._flood_on is not None and isinstance(request, self._flood_on):
            err = FloodWaitError.__new__(FloodWaitError)
            err.seconds = self._flood_seconds
            raise err
        if isinstance(request, ImportContactsRequest):
            return FakeResult(list(self._imported))
        if isinstance(request, DeleteContactsRequest):
            return FakeResult(list(self._deleted))
        return None  # AddContactRequest result is unused

    def of_type(self, request_type: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, request_type)]


def make(client: FakeClient) -> TelegramContactResolver:
    return TelegramContactResolver(client)  # type: ignore[arg-type]


PHONE = "79001234567"
# What import reports: the contact-list name we just set (the placeholder).
IMPORTED = FakeUser(7, "neo", PHONE, "")
# What delete reports: the name the person set on their own profile.
REAL = FakeUser(7, "neo", "Thomas", "Anderson")


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
    async def test_profile_uses_the_telegram_name_not_the_contact_name(
        self,
    ) -> None:
        client = FakeClient(imported=[IMPORTED], deleted=[REAL])

        profile = await make(client).resolve(PHONE)

        assert profile == Profile(
            user_id=7, username="neo", firstname="Thomas", lastname="Anderson"
        )

    @pytest.mark.asyncio
    async def test_contact_is_resaved_under_the_telegram_name(self) -> None:
        client = FakeClient(imported=[IMPORTED], deleted=[REAL])
        await make(client).resolve(PHONE)

        (add,) = client.of_type(AddContactRequest)
        assert add.first_name == "Thomas"
        assert add.last_name == "Anderson"
        assert add.phone == "+" + PHONE

    @pytest.mark.asyncio
    async def test_import_uses_the_phone_as_placeholder_name(self) -> None:
        client = FakeClient(imported=[IMPORTED], deleted=[REAL])
        await make(client).resolve(PHONE)

        contact = client.of_type(ImportContactsRequest)[0].contacts[0]
        assert contact.first_name == PHONE
        assert contact.phone == "+" + PHONE

    @pytest.mark.asyncio
    async def test_no_users_means_absent_and_no_further_calls(self) -> None:
        client = FakeClient(imported=[])
        assert await make(client).resolve(PHONE) is None
        assert client.of_type(DeleteContactsRequest) == []
        assert client.of_type(AddContactRequest) == []

    @pytest.mark.asyncio
    async def test_missing_from_delete_response_falls_back_to_import(
        self,
    ) -> None:
        client = FakeClient(imported=[IMPORTED], deleted=[])

        profile = await make(client).resolve(PHONE)

        assert profile is not None
        assert profile.firstname == PHONE  # degraded, but still resolved

    @pytest.mark.asyncio
    async def test_nameless_account_is_not_resaved(self) -> None:
        client = FakeClient(
            imported=[IMPORTED], deleted=[FakeUser(7, None, None, None)]
        )

        profile = await make(client).resolve(PHONE)

        assert profile == Profile(user_id=7)
        assert client.of_type(AddContactRequest) == []

    @pytest.mark.asyncio
    async def test_flood_wait_on_import_becomes_flood_error(self) -> None:
        client = FakeClient(flood_on=ImportContactsRequest)
        with pytest.raises(FloodError) as exc_info:
            await make(client).resolve(PHONE)
        assert exc_info.value.seconds == 42

    @pytest.mark.asyncio
    async def test_flood_wait_mid_flow_becomes_flood_error(self) -> None:
        client = FakeClient(
            imported=[IMPORTED], flood_on=DeleteContactsRequest
        )
        with pytest.raises(FloodError):
            await make(client).resolve(PHONE)


class TestClientId:
    def test_is_deterministic_per_phone(self) -> None:
        a = TelegramContactResolver._client_id("79001234567")
        b = TelegramContactResolver._client_id("79001234567")
        c = TelegramContactResolver._client_id("79009876543")
        assert a == b
        assert a != c
