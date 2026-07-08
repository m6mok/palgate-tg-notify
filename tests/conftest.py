import pytest
from unittest.mock import Mock, AsyncMock
from typing import Any, Dict, List
from requests import Response

from src.models import ItemResponse
from src.main import Settings, HttpClient, LogUpdater


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


@pytest.fixture
def mock_settings() -> Settings:
    """
    Mock settings for testing.

    Returns:
        Settings: A Settings instance with test values
    """
    return Settings(
        DEVICE_ID="test_device",
        USER_ID=12345,
        SESSION_TOKEN="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",  # 16 bytes hex string (32 hex chars)
        SESSION_TOKEN_TYPE=0,  # Use enum value instead of string
        URL_USER_LOG="https://example.com/log/{device_id}",
        TZ=3,
        TELEGRAM_API_TOKEN="test_token",
        TELEGRAM_CHAT_ID=123456789,
        TELEGRAM_LOG_CHAT_ID=987654321,
        CRON_DELAY=60
    )


@pytest.fixture
def sample_log_item_data() -> Dict[str, Any]:
    """
    Sample log item data for testing.

    Returns:
        Dict[str, Any]: A dictionary representing a log item
    """
    return BASE_LOG_ITEM_DATA.copy()


@pytest.fixture
def sample_item_response_data() -> Dict[str, Any]:
    """
    Sample item response data for testing.

    Returns:
        Dict[str, Any]: A dictionary representing an item response
    """
    return {
        "log": [BASE_LOG_ITEM_DATA.copy(), SECOND_LOG_ITEM_DATA.copy()],
        "err": False,
        "msg": "Success",
        "status": "ok"
    }


@pytest.fixture
def mock_http_client() -> HttpClient:
    """
    Mock HTTP client for testing.

    Returns:
        HttpClient: An HttpClient instance with mocked __get method
    """
    client = HttpClient()
    client._HttpClient__get = Mock()  # type: ignore
    return client


@pytest.fixture
def mock_cache() -> AsyncMock:
    """
    Mock cache for testing.

    Returns:
        AsyncMock: An async mock with get, add, and set methods
    """
    cache = AsyncMock()
    cache.get = AsyncMock()
    cache.add = AsyncMock()
    cache.set = AsyncMock()
    return cache


@pytest.fixture
def mock_logger() -> Mock:
    """
    Mock logger for testing.

    Returns:
        Mock: A mock logger with debug, info, and error methods
    """
    logger = Mock()
    logger.debug = Mock()
    logger.info = Mock()
    logger.error = Mock()
    return logger


@pytest.fixture
def mock_log_updater(mock_settings: Settings, mock_logger: Mock, mock_cache: AsyncMock) -> LogUpdater:
    """
    Mock LogUpdater instance for testing.

    Returns:
        LogUpdater: A LogUpdater instance with mocked dependencies
    """
    chat_logger = mock_logger
    log_logger = mock_logger
    return LogUpdater(mock_settings, chat_logger, log_logger, mock_cache)


@pytest.fixture
def mock_http_response() -> Mock:
    """
    Mock HTTP response for testing.

    Returns:
        Mock: A mock Response object
    """
    response = Mock(spec=Response)
    response.raise_for_status = Mock()
    response.json = Mock()
    return response


@pytest.fixture
def mock_log_item() -> Mock:
    """
    Mock log item for testing.

    Returns:
        Mock: A mock log item with model_dump method
    """
    log_item = Mock()
    log_item.model_dump = Mock(return_value=BASE_LOG_ITEM_DATA.copy())
    return log_item


@pytest.fixture
def mock_item_response() -> Mock:
    """
    Mock item response for testing.

    Returns:
        Mock: A mock ItemResponse object
    """
    response = Mock(spec=ItemResponse)
    response.log = []
    response.err = False
    response.msg = "Success"
    response.status = "ok"
    return response


@pytest.fixture
def sample_log_items() -> List[Dict[str, Any]]:
    """
    Sample list of log items for testing.

    Returns:
        List[Dict[str, Any]]: A list of log item dictionaries
    """
    return [
        BASE_LOG_ITEM_DATA.copy(),
        SECOND_LOG_ITEM_DATA.copy(),
        THIRD_LOG_ITEM_DATA.copy()
    ]
