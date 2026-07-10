"""One-time interactive login to create the Telethon user session.

Run this once on a machine where you can type the login code and 2FA
password. It writes a ``<TG_SESSION>.session`` file that the service then
uses read-only (it never logs in on its own). Point TG_SESSION at the data
volume so the session survives redeploys, e.g. ``data/telethon``.

    TG_API_ID=... TG_API_HASH=... TG_SESSION=data/telethon \
        uv run python scripts/telethon_login.py

or, with the values already in .dev.env:  make login
"""

import asyncio
import os

from telethon import TelegramClient


async def main() -> None:
    try:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
    except (KeyError, ValueError) as err:
        raise SystemExit(
            "Set TG_API_ID and TG_API_HASH (from https://my.telegram.org)"
        ) from err
    session = os.environ.get("TG_SESSION", "data/telethon")

    client = TelegramClient(session, api_id, api_hash)
    await client.start()  # prompts for phone, login code, and 2FA password
    me = await client.get_me()
    handle = getattr(me, "username", None) or getattr(me, "id", "?")
    print("Authorized as %s; session written to %s.session" % (handle, session))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
