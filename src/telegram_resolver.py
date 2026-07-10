"""Raw phone → Telegram profile lookup via a Telethon user session.

This is the only component that talks to Telegram's MTProto API. It imports a
phone number as a contact (``contacts.importContacts``) — the same mechanism
the mobile app uses when you "dive into" a number — and reads back the profile
if the target's privacy allows it. Imported contacts are left in place.

It is deliberately thin: all caching, rate limiting and FloodWait handling
live in ``resolver.CachingResolver``, which wraps this class. A FloodWait is
translated into a ``FloodError`` so the anti-flood layer can react without
knowing about Telethon. The session must already be authorized —
``connect`` never triggers an interactive login (see ``scripts/telethon_login.py``).
"""

from hashlib import blake2b
from logging import getLogger

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from resolver import FloodError, Profile


class TelegramContactResolver:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._log = getLogger("default")

    @staticmethod
    def build(
        session: str, api_id: int, api_hash: str
    ) -> "TelegramContactResolver":
        return TelegramContactResolver(
            TelegramClient(session, api_id, api_hash)
        )

    async def connect(self) -> bool:
        """Connect and confirm the session is authorized; no login prompt."""
        await self._client.connect()
        if not await self._client.is_user_authorized():
            self._log.error(
                "Telegram resolver session is not authorized; disabling"
            )
            await self._client.disconnect()
            return False
        return True

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def resolve(self, phone: str) -> Profile | None:
        contact = InputPhoneContact(
            client_id=self._client_id(phone),
            phone="+" + phone,
            first_name="gate",
            last_name="",
        )
        try:
            result = await self._client(ImportContactsRequest([contact]))
        except FloodWaitError as err:
            raise FloodError(float(err.seconds)) from err

        users = result.users
        if not users:
            return None
        user = users[0]
        return Profile(
            user_id=int(user.id),
            username=user.username,
            firstname=user.first_name,
            lastname=user.last_name,
        )

    @staticmethod
    def _client_id(phone: str) -> int:
        # importContacts needs a caller-unique client_id per contact; a stable
        # hash of the phone keeps repeated lookups deterministic.
        digest = blake2b(phone.encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big", signed=True)
