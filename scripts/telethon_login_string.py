"""One-time interactive login that prints a Telethon StringSession.

Use this for a headless / release server: run it once on a machine where you
can type the login code and 2FA password, then paste the printed string into
the server's env file as TG_SESSION_STRING (alongside TG_API_ID / TG_API_HASH
and RESOLVE_ENABLED=true). No session file has to be copied onto the volume.

    TG_API_ID=... TG_API_HASH=... uv run python scripts/telethon_login_string.py

or, with the values already in .dev.env:  make login-string

Treat the printed string like a password — it grants full access to the
account. Use the SAME TG_API_ID / TG_API_HASH at runtime, and do not run the
same session on two machines at once.
"""

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    try:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
    except (KeyError, ValueError) as err:
        raise SystemExit(
            "Set TG_API_ID and TG_API_HASH (from https://my.telegram.org)"
        ) from err

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # prompts for phone, login code, and 2FA password
    session_string = client.session.save()
    await client.disconnect()

    print("\nTG_SESSION_STRING=%s\n" % session_string)
    print("Paste the line above into the server env file. Keep it secret.")


if __name__ == "__main__":
    asyncio.run(main())
