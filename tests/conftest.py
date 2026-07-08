from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
from pylgate.types import TokenType

from config import Settings
from models import ItemResponse
from notify import NotifyError
from palgate import TransientFetchError


# Base test data constants to reduce duplication
BASE_LOG_ITEM_DATA = {
    "userId": "12345",
    "operation": "call",
    "time": 1708675200,  # 2024-02-23 00:00:00 UTC
    "firstname": "John",
    "lastname": "Doe",
    "image": True,
    "reason": 0,
    "type": 1,  # CALL
    "sn": "79001234567"
}

SECOND_LOG_ITEM_DATA = {
    "userId": "67890",
    "operation": "admin",
    "time": 1708675300,
    "firstname": "Jane",
    "lastname": "Smith",
    "image": False,
    "reason": 1,
    "type": 100,  # ADMIN
    "sn": "79009876543"
}

THIRD_LOG_ITEM_DATA = {
    "userId": "11111",
    "operation": "call",
    "time": 1708675400,
    "firstname": "Bob",
    "lastname": "Johnson",
    "image": True,
    "reason": 0,
    "type": 1,
    "sn": "79001111111"
}

SESSION_TOKEN_HEX = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"  # 16 bytes


def make_response(*items: Dict[str, Any]) -> ItemResponse:
    """Build a validated ItemResponse; pass items newest-first."""
    return ItemResponse.model_validate(
        {
            "log": [item.copy() for item in items],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }
    )


class RecordingNotifier:
    """Notifier test double: records deliveries, fails on demand."""

    def __init__(self, name: str = "recording") -> None:
        self._name = name
        self.sent: List[str] = []
        self.fail_with: NotifyError | None = None

    @property
    def name(self) -> str:
        return self._name

    async def send(self, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(text)


class ScriptedPalgateClient:
    """PalgateClient test double replaying a script of results.

    Script entries are ItemResponse objects (returned) or exceptions
    (raised). When the script runs dry, ``on_empty`` is called (e.g. to
    set a stop event) and a transient error is raised.
    """

    def __init__(self, script: List[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.on_empty: Callable[[], None] | None = None

    async def fetch_log(self) -> ItemResponse:
        self.calls += 1
        if not self.script:
            if self.on_empty is not None:
                self.on_empty()
            raise TransientFetchError("script exhausted")
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, ItemResponse)
        return result


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        DEVICE_ID="test_device",
        USER_ID=12345,
        SESSION_TOKEN=SESSION_TOKEN_HEX,
        SESSION_TOKEN_TYPE=TokenType.SMS,
        URL_USER_LOG="https://example.com/log/{device_id}",
        TZ=3,
        TELEGRAM_API_TOKEN="test_token",
        TELEGRAM_CHAT_ID=123456789,
        TELEGRAM_LOG_CHAT_ID=987654321,
        CRON_DELAY=60,
        STATE_FILE=str(tmp_path / "state.json"),
        HEARTBEAT_FILE=str(tmp_path / "heartbeat"),
    )


@pytest.fixture
def sample_log_item_data() -> Dict[str, Any]:
    return BASE_LOG_ITEM_DATA.copy()


@pytest.fixture
def sample_item_response_data() -> Dict[str, Any]:
    return {
        "log": [BASE_LOG_ITEM_DATA.copy(), SECOND_LOG_ITEM_DATA.copy()],
        "err": False,
        "msg": "Success",
        "status": "ok"
    }


@pytest.fixture
def sample_log_items() -> List[Dict[str, Any]]:
    return [
        BASE_LOG_ITEM_DATA.copy(),
        SECOND_LOG_ITEM_DATA.copy(),
        THIRD_LOG_ITEM_DATA.copy()
    ]
