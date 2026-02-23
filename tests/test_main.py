import pytest
from unittest.mock import Mock, patch, AsyncMock
from requests import Response, HTTPError, ReadTimeout
from requests.exceptions import JSONDecodeError
from pydantic import ValidationError

from src.main import HttpClient, LogUpdater
from src.models import ItemResponse


class TestHttpClient:
    """Test cases for HttpClient class."""

    def test_initialization_with_default_parameters(self) -> None:
        """Test HttpClient initialization with default parameters."""
        client = HttpClient()
        assert client._HttpClient__timeout == 5
        assert client._HttpClient__tries == 3
        assert client._HttpClient__delay == 1
        assert client._HttpClient__backoff == 2

    def test_initialization_with_custom_parameters(self) -> None:
        """Test HttpClient initialization with custom parameters."""
        client = HttpClient(timeout=10, tries=5, delay=2, backoff=3)
        assert client._HttpClient__timeout == 10
        assert client._HttpClient__tries == 5
        assert client._HttpClient__delay == 2
        assert client._HttpClient__backoff == 3

    @patch('src.main.requests_get')
    def test_get_method_makes_successful_request(self, mock_requests_get: Mock) -> None:
        """Test get method makes successful HTTP GET request."""
        # Mock response
        mock_response = Mock(spec=Response)
        mock_response.raise_for_status.return_value = None
        mock_requests_get.return_value = mock_response

        client = HttpClient()
        url = "https://example.com"
        headers = {"User-Agent": "test"}

        response = client.get(url, headers)

        mock_requests_get.assert_called_once_with(url, headers=headers, timeout=5)
        assert response == mock_response

    @patch('src.main.retry_call')
    def test_get_method_uses_retry_mechanism(self, mock_retry_call: Mock) -> None:
        """Test get method uses retry mechanism with correct parameters."""
        mock_response = Mock(spec=Response)
        mock_retry_call.return_value = mock_response

        client = HttpClient()
        url = "https://example.com"
        headers = {"User-Agent": "test"}

        response = client.get(url, headers)

        mock_retry_call.assert_called_once()
        call_args = mock_retry_call.call_args
        assert call_args[0][0] == client._HttpClient__get
        assert call_args[0][1] == (url, headers)
        assert call_args[1]['exceptions'] == (HTTPError, ReadTimeout)
        assert call_args[1]['tries'] == 3
        assert call_args[1]['delay'] == 1
        assert call_args[1]['backoff'] == 2


class TestLogUpdater:
    """Test cases for LogUpdater class."""

    def test_initialization_sets_correct_attributes(self, mock_log_updater: LogUpdater) -> None:
        """Test LogUpdater initialization sets all required attributes."""
        assert mock_log_updater._LogUpdater__url == "https://example.com/log/test_device"
        assert mock_log_updater._LogUpdater__headers == {"User-Agent": "okhttp/4.9.3"}
        assert mock_log_updater._LogUpdater__cron_delay == 60
        assert mock_log_updater._LogUpdater__session_token == b'\xa1\xb2\xc3\xd4\xe5\xf6\xa1\xb2\xc3\xd4\xe5\xf6\xa1\xb2\xc3\xd4'
        assert mock_log_updater._LogUpdater__user_id == 12345
        assert mock_log_updater._LogUpdater__session_token_type == 0  # TokenType.SMS enum value

    def test_cron_delay_property_returns_correct_value(self, mock_log_updater: LogUpdater) -> None:
        """Test cron_delay property returns the configured delay."""
        assert mock_log_updater.cron_delay == 60

    @patch('src.main.generate_token')
    def test_get_token_generates_correct_token(self, mock_generate_token: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_token method generates token with correct parameters."""
        mock_generate_token.return_value = "test_token"

        token = mock_log_updater.get_token()

        mock_generate_token.assert_called_once_with(
            b'\xa1\xb2\xc3\xd4\xe5\xf6\xa1\xb2\xc3\xd4\xe5\xf6\xa1\xb2\xc3\xd4', 12345, 0
        )
        assert token == "test_token"

    @pytest.mark.asyncio
    async def test_cache_operations(self, mock_log_updater: LogUpdater, mock_cache: AsyncMock) -> None:
        """Test all cache operations: get, add, and set last log item."""
        test_item = Mock()

        # Test get_last_log_item - retrieve cached item
        mock_cache.get.return_value = "test_item"
        result = await mock_log_updater.get_last_log_item()
        mock_cache.get.assert_called_with("last_log_item", None)
        assert result == "test_item"

        # Test add_last_log_item - add new item to cache
        await mock_log_updater.add_last_log_item(test_item)
        mock_cache.add.assert_called_with("last_log_item", test_item)

        # Test set_last_log_item - update existing item in cache
        await mock_log_updater.set_last_log_item(test_item)
        mock_cache.set.assert_called_with("last_log_item", test_item)

        # Verify all cache methods were called exactly once
        assert mock_cache.get.call_count == 1
        assert mock_cache.add.call_count == 1
        assert mock_cache.set.call_count == 1

    @patch.object(HttpClient, 'get')
    def test_get_items_returns_valid_response(self, mock_http_get: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_items method successfully fetches and parses log items."""
        # Mock successful HTTP response with valid log data
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [{
                "userId": "123",
                "operation": "call",
                "time": 1708675200,
                "firstname": "John",
                "lastname": "Doe",
                "image": True,
                "reason": 0,
                "type": 1,
                "sn": "79001234567"
            }],
            "err": False,
            "msg": "Success",
            "status": "ok"
        }
        mock_http_get.return_value = mock_response

        # Execute the method under test
        result = mock_log_updater.get_items()

        # Verify authentication token was added to headers
        assert "X-Bt-Token" in mock_log_updater._LogUpdater__headers

        # Verify HTTP request was made
        mock_http_get.assert_called_once()

        # Verify response was correctly parsed
        assert result.err is False
        assert result.msg == "Success"
        assert result.status == "ok"
        assert len(result.log) == 1
        assert result.log[0].userId == "123"

    @patch.object(HttpClient, 'get')
    def test_get_items_handles_http_error(self, mock_http_get: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_items method handles HTTP errors and logs them."""
        mock_http_get.side_effect = HTTPError("Connection error")
        mock_log_updater._LogUpdater__log.error.reset_mock()

        with pytest.raises(HTTPError):
            mock_log_updater.get_items()

        mock_log_updater._LogUpdater__log.error.assert_called_once_with("HTTP failed: Connection error")

    @patch.object(HttpClient, 'get')
    def test_get_items_handles_json_decode_error(self, mock_http_get: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_items method handles JSON decode errors and logs them."""
        mock_response = Mock(spec=Response)
        mock_response.json.side_effect = JSONDecodeError("Invalid JSON", "doc", 0)
        mock_http_get.return_value = mock_response
        mock_log_updater._LogUpdater__log.error.reset_mock()

        with pytest.raises(JSONDecodeError):
            mock_log_updater.get_items()

        mock_log_updater._LogUpdater__log.error.assert_called_once()
        actual_log_call = mock_log_updater._LogUpdater__log.error.call_args[0][0]
        assert "JSON decode error:" in actual_log_call

    @patch.object(HttpClient, 'get')
    def test_get_items_handles_validation_error(self, mock_http_get: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_items method handles validation errors and logs them."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {"invalid": "data"}
        mock_http_get.return_value = mock_response

        with pytest.raises(ValidationError):
            mock_log_updater.get_items()

        mock_log_updater._LogUpdater__log.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_save_handles_exceptions_gracefully(self, mock_log_updater: LogUpdater) -> None:
        """Test update_new_items_save method handles exceptions without crashing."""
        mock_log_updater._LogUpdater__update_new_items = AsyncMock(side_effect=Exception("Test error"))

        # Should not raise exception
        await mock_log_updater.update_new_items_save()

        # Method should be called but exception should be caught
        mock_log_updater._LogUpdater__update_new_items.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(LogUpdater, 'get_items')
    async def test_update_new_items_adds_first_item_when_no_last_item_exists(
        self,
        mock_get_items: Mock,
        mock_log_updater: LogUpdater,
        mock_cache: AsyncMock,
        mock_log_item: Mock
    ) -> None:
        """Test __update_new_items adds first item to cache when no last item exists."""
        # Create mock log items
        mock_item1 = mock_log_item
        mock_item2 = Mock()
        mock_item2.model_dump = Mock(return_value={
            "userId": "456",
            "operation": "call",
            "time": 1708675300,
            "firstname": "Jane",
            "lastname": "Smith",
            "image": False,
            "reason": 1,
            "type": 100,
            "sn": "79009876543"
        })

        # Mock response with log items
        mock_response = Mock(spec=ItemResponse)
        mock_response.log = [mock_item1, mock_item2]
        mock_get_items.return_value = mock_response

        mock_cache.get.return_value = None  # No last item

        await mock_log_updater._LogUpdater__update_new_items()

        # Should add first log item to cache
        mock_cache.add.assert_called_once()
        mock_log_updater._LogUpdater__log.debug.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(LogUpdater, 'get_items')
    async def test_update_new_items_logs_new_items_and_updates_cache(
        self,
        mock_get_items: Mock,
        mock_log_updater: LogUpdater,
        mock_cache: AsyncMock
    ) -> None:
        """Test __update_new_items logs new items and updates cache when last item exists."""
        # Create mock log items
        item1 = Mock()
        item2 = Mock()
        item3 = Mock()

        # Mock response with log items
        mock_response = Mock(spec=ItemResponse)
        mock_response.log = [item1, item2, item3]
        mock_get_items.return_value = mock_response

        # Mock last item (item2)
        mock_cache.get.return_value = item2

        # Mock Item.from_log_item to return the same item
        with patch('src.main.Item.from_log_item', side_effect=lambda x: x):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should log new items (item1) and set first item as last
        mock_log_updater._LogUpdater__chat.info.assert_called_once()
        mock_cache.set.assert_called_once_with("last_log_item", item1)

    @pytest.mark.asyncio
    @patch.object(LogUpdater, 'get_items')
    async def test_update_new_items_raises_error_for_empty_log(
        self,
        mock_get_items: Mock,
        mock_log_updater: LogUpdater
    ) -> None:
        """Test __update_new_items raises ValueError for empty log."""
        # Mock response with empty log
        mock_response = Mock(spec=ItemResponse)
        mock_response.log = []
        mock_get_items.return_value = mock_response

        with pytest.raises(ValueError, match="Wrong log list:"):
            await mock_log_updater._LogUpdater__update_new_items()

    @pytest.mark.asyncio
    @patch.object(LogUpdater, 'get_items')
    async def test_update_new_items_handles_single_item_with_no_last_item(
        self,
        mock_get_items: Mock,
        mock_log_updater: LogUpdater,
        mock_cache: AsyncMock,
        mock_log_item: Mock
    ) -> None:
        """Test __update_new_items handles single item when no last item exists."""
        # Mock response with single item
        mock_response = Mock(spec=ItemResponse)
        mock_response.log = [mock_log_item]
        mock_get_items.return_value = mock_response

        mock_cache.get.return_value = None  # No last item

        await mock_log_updater._LogUpdater__update_new_items()

        # Should add the single item to cache
        mock_cache.add.assert_called_once()
        mock_log_updater._LogUpdater__log.debug.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(LogUpdater, 'get_items')
    async def test_update_new_items_logs_all_items_when_all_are_new(
        self,
        mock_get_items: Mock,
        mock_log_updater: LogUpdater,
        mock_cache: AsyncMock
    ) -> None:
        """Test __update_new_items logs all items when all items are new."""
        # Create mock log items
        item1 = Mock()
        item2 = Mock()
        item3 = Mock()

        # Mock response with log items
        mock_response = Mock(spec=ItemResponse)
        mock_response.log = [item1, item2, item3]
        mock_get_items.return_value = mock_response

        # Mock last item that doesn't match any (all items are new)
        mock_cache.get.return_value = Mock()

        # Mock Item.from_log_item to return the same item
        with patch('src.main.Item.from_log_item', side_effect=lambda x: x):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should log all items and set first item as last
        mock_log_updater._LogUpdater__chat.info.assert_called_once()
        mock_cache.set.assert_called_once_with("last_log_item", item1)

    @patch.object(HttpClient, 'get')
    def test_get_items_preserves_custom_headers(
        self,
        mock_http_get: Mock,
        mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method preserves custom headers while adding token."""
        # Mock response
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [{
                "userId": "123",
                "operation": "call",
                "time": 1708675200,
                "firstname": "John",
                "lastname": "Doe",
                "image": True,
                "reason": 0,
                "type": 1,
                "sn": "79001234567"
            }],
            "err": False,
            "msg": "Success",
            "status": "ok"
        }
        mock_http_get.return_value = mock_response

        # Add custom header before calling get_items
        mock_log_updater._LogUpdater__headers["Custom-Header"] = "custom_value"

        result = mock_log_updater.get_items()

        # Check that custom header is preserved and token is added
        assert "Custom-Header" in mock_log_updater._LogUpdater__headers
        assert "X-Bt-Token" in mock_log_updater._LogUpdater__headers
        mock_http_get.assert_called_once()
        assert result.err is False


# Additional edge case tests
class TestHttpClientEdgeCases:
    """Test cases for edge cases in HttpClient class."""

    @patch('src.main.requests_get')
    def test_get_method_with_empty_headers(self, mock_requests_get: Mock) -> None:
        """Test get method works with empty headers."""
        mock_response = Mock(spec=Response)
        mock_response.raise_for_status.return_value = None
        mock_requests_get.return_value = mock_response

        client = HttpClient()
        url = "https://example.com"
        headers = {}  # Empty headers

        response = client.get(url, headers)

        mock_requests_get.assert_called_once_with(url, headers=headers, timeout=5)
        assert response == mock_response

    @patch('src.main.retry_call')
    def test_get_method_with_custom_retry_parameters(self, mock_retry_call: Mock) -> None:
        """Test get method uses custom retry parameters."""
        mock_response = Mock(spec=Response)
        mock_retry_call.return_value = mock_response

        client = HttpClient(timeout=10, tries=5, delay=2, backoff=3)
        url = "https://example.com"
        headers = {"User-Agent": "test"}

        response = client.get(url, headers)

        mock_retry_call.assert_called_once()
        call_args = mock_retry_call.call_args
        assert call_args[1]['tries'] == 5
        assert call_args[1]['delay'] == 2
        assert call_args[1]['backoff'] == 3


class TestLogUpdaterEdgeCases:
    """Test cases for edge cases in LogUpdater class."""

    @patch.object(HttpClient, 'get')
    def test_get_items_with_empty_response_log(self, mock_http_get: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_items method with empty log in response."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [],
            "err": False,
            "msg": "Success",
            "status": "ok"
        }
        mock_http_get.return_value = mock_response

        with pytest.raises(ValidationError):
            mock_log_updater.get_items()

    @pytest.mark.asyncio
    @patch.object(LogUpdater, 'get_items')
    async def test_update_new_items_with_identical_last_item(
        self,
        mock_get_items: Mock,
        mock_log_updater: LogUpdater,
        mock_cache: AsyncMock
    ) -> None:
        """Test __update_new_items when last item is identical to first item."""
        # Create mock log items
        item1 = Mock()
        item2 = Mock()
        item3 = Mock()

        # Mock response with log items
        mock_response = Mock(spec=ItemResponse)
        mock_response.log = [item1, item2, item3]
        mock_get_items.return_value = mock_response

        # Mock last item (item1 - identical to first item)
        mock_cache.get.return_value = item1

        # Mock Item.from_log_item to return the same item
        with patch('src.main.Item.from_log_item', side_effect=lambda x: x):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should not log any items since there are no new items
        mock_log_updater._LogUpdater__chat.info.assert_not_called()
        # Should not update cache since no new items
        mock_cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_new_items_save_handles_specific_exception_types(self, mock_log_updater: LogUpdater) -> None:
        """Test update_new_items_save gracefully handles HTTP errors and generic exceptions."""
        # Test handling of HTTPError
        mock_update_http = AsyncMock(side_effect=HTTPError("Test HTTP error"))
        mock_log_updater._LogUpdater__update_new_items = mock_update_http
        await mock_log_updater.update_new_items_save()  # Should not raise

        # Test handling of generic Exception
        mock_update_generic = AsyncMock(side_effect=Exception("Test generic error"))
        mock_log_updater._LogUpdater__update_new_items = mock_update_generic
        await mock_log_updater.update_new_items_save()  # Should not raise

        # Verify both exceptions were caught and handled
        assert mock_update_http.call_count == 1
        assert mock_update_generic.call_count == 1

    @patch.object(HttpClient, 'get')
    def test_get_items_preserves_existing_token_header(self, mock_http_get: Mock, mock_log_updater: LogUpdater) -> None:
        """Test get_items method preserves existing X-Bt-Token header."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [{
                "userId": "123",
                "operation": "call",
                "time": 1708675200,
                "firstname": "John",
                "lastname": "Doe",
                "image": True,
                "reason": 0,
                "type": 1,
                "sn": "79001234567"
            }],
            "err": False,
            "msg": "Success",
            "status": "ok"
        }
        mock_http_get.return_value = mock_response

        # Add existing token header
        mock_log_updater._LogUpdater__headers["X-Bt-Token"] = "existing_token"

        result = mock_log_updater.get_items()

        # Check that token was updated (not just added)
        assert mock_log_updater._LogUpdater__headers["X-Bt-Token"] != "existing_token"
        mock_http_get.assert_called_once()
        assert result.err is False

    @pytest.mark.asyncio
    async def test_cache_methods_with_none_items(self, mock_log_updater: LogUpdater, mock_cache: AsyncMock) -> None:
        """Test cache methods handle None items correctly."""
        # Test get_last_log_item with None from cache
        mock_cache.get.return_value = None
        result = await mock_log_updater.get_last_log_item()
        assert result is None

        # Test add_last_log_item with None (should still call cache)
        await mock_log_updater.add_last_log_item(None)
        mock_cache.add.assert_called_once_with("last_log_item", None)

        # Test set_last_log_item with None (should still call cache)
        await mock_log_updater.set_last_log_item(None)
        mock_cache.set.assert_called_once_with("last_log_item", None)
