from pydantic import Field, field_validator
from pydantic_settings import BaseSettings
from pylgate.types import TokenType


class Settings(BaseSettings):
    """Runtime configuration; every field without a default is required.

    Validation happens at startup so a broken configuration crashes
    immediately instead of surfacing as a runtime error hours later.
    """

    DEVICE_ID: str
    USER_ID: int
    SESSION_TOKEN: str
    SESSION_TOKEN_TYPE: TokenType
    URL_USER_LOG: str
    TZ: int
    TELEGRAM_API_TOKEN: str
    TELEGRAM_CHAT_ID: int
    TELEGRAM_LOG_CHAT_ID: int
    CRON_DELAY: int = Field(ge=0)

    # Optional Max messenger channel; enabled only when the token is set.
    MAX_API_TOKEN: str = ""
    MAX_CHAT_ID: int = 0

    # Optional /rollback support; a PAT with Actions read+write and
    # Contents read on GITHUB_REPO. Empty disables the command.
    GITHUB_TOKEN: str = ""
    GITHUB_REPO: str = "m6mok/palgate-tg-notify"

    STATE_FILE: str = "data/state.json"
    HEARTBEAT_FILE: str = "data/heartbeat"
    VERSION_FILE: str = "data/version"
    LOCK_TIMEOUT: float = 60
    MAX_BACKOFF: float = 300
    ALERT_AFTER_FAILURES: int = Field(default=10, ge=1)

    # Optional Telegram identity enrichment: resolve a log entry's phone
    # number to a Telegram profile (via a user account / MTProto) and edit
    # the notification to append it. Disabled unless RESOLVE_ENABLED is set
    # and a valid, already-authorized session is present.
    RESOLVE_ENABLED: bool = False
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION: str = "data/telethon"
    RESOLVER_STATE_FILE: str = "data/resolver.json"
    # Anti-flood knobs — importContacts is rate-limited hard, so keep these
    # conservative. Spacing plus rolling hourly/daily caps; TTLs favour a
    # long-lived positive cache and a short negative one.
    RESOLVE_MIN_INTERVAL: float = Field(default=5, ge=0)
    RESOLVE_PER_HOUR: int = Field(default=20, ge=1)
    RESOLVE_PER_DAY: int = Field(default=150, ge=1)
    RESOLVE_POSITIVE_TTL: float = Field(default=30 * 86400, ge=0)
    RESOLVE_NEGATIVE_TTL: float = Field(default=3 * 86400, ge=0)
    RESOLVE_POLL_INTERVAL: float = Field(default=5, ge=1)

    @field_validator("SESSION_TOKEN")
    @classmethod
    def session_token_must_be_hex(cls, value: str) -> str:
        try:
            bytes.fromhex(value)
        except ValueError as err:
            raise ValueError(
                "SESSION_TOKEN must be a hex string: %s" % err
            ) from err
        return value

    @field_validator("URL_USER_LOG")
    @classmethod
    def url_must_have_device_placeholder(cls, value: str) -> str:
        if "{device_id}" not in value:
            raise ValueError(
                "URL_USER_LOG must contain a {device_id} placeholder"
            )
        return value

    @property
    def session_token_bytes(self) -> bytes:
        return bytes.fromhex(self.SESSION_TOKEN)
