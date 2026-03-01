from enum import Enum

from pydantic_settings import BaseSettings

from pylgate.types import TokenType


class Environment(Enum):
    DEV = "dev"
    STABLE = "stable"


class Settings(BaseSettings):
    DEVICE_ID: str
    USER_ID: int
    SESSION_TOKEN: str
    SESSION_TOKEN_TYPE: TokenType
    URL_USER_LOG: str
    TZ: int
    TELEGRAM_API_TOKEN: str
    TELEGRAM_CHAT_ID: int
    TELEGRAM_LOG_CHAT_ID: int
    CRON_DELAY: int
    ENVIRONMENT: Environment = Environment.DEV
