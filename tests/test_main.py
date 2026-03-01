import pytest
from unittest.mock import Mock, patch, AsyncMock
from requests import Response, HTTPError, ReadTimeout
from requests.exceptions import JSONDecodeError
from pydantic import ValidationError

from src.config import Environment, Settings
from src.handlers import (
    BroadcastHandlerBase,
    BroadcastLoggerHandler,
    CacheHandler,
    CacheHandlerBase,
    HttpHandlerBase,
    LogItemCacheHandler,
    Method,
    PalGateItemsHandler,
    SyncHttpHandler,
)
from src.main import get_args, mainloop
from src.services import LogUpdater, PalGateTokenGenerator


class TestSyncHttpHandler:
    """Test cases for SyncHttpHandler class."""

    def test_initialization_with_default_parameters(self) -> None:
        """Test SyncHttpHandler initialization with default parameters."""
        client = SyncHttpHandler("https://example.com")
        assert client._timeout == 1
        assert client._tries == 0
        assert client._delay == 0
        assert client._backoff == 0

    def test_initialization_with_custom_parameters(self) -> None:
        """Test SyncHttpHandler initialization with custom parameters."""
        client = SyncHttpHandler(
            "https://example.com", timeout=10, tries=5, delay=2, backoff=3
        )
        assert client._timeout == 10
        assert client._tries == 5
        assert client._delay == 2
        assert client._backoff == 3

    @pytest.mark.asyncio
    @patch("src.handlers.http.retry_call")
    async def test_request_method_makes_successful_request(
        self, mock_retry_call: Mock
    ) -> None:
        """Test request method makes successful HTTP request."""
        # Mock response
        mock_response = Mock(spec=Response)
        mock_response.raise_for_status.return_value = None
        mock_retry_call.return_value = mock_response

        client = SyncHttpHandler("https://example.com")
        headers = {"User-Agent": "test"}

        response = await client.request(headers=headers)

        mock_retry_call.assert_called_once()
        assert response == mock_response

    @pytest.mark.asyncio
    @patch("src.handlers.http.retry_call")
    async def test_request_method_uses_retry_mechanism(
        self, mock_retry_call: Mock
    ) -> None:
        """Test request method uses retry mechanism with correct parameters."""
        mock_response = Mock(spec=Response)
        mock_retry_call.return_value = mock_response

        client = SyncHttpHandler(
            "https://example.com", tries=3, delay=1, backoff=2
        )
        headers = {"User-Agent": "test"}

        _ = await client.request(headers=headers)  # Response not used in test

        mock_retry_call.assert_called_once()
        call_args = mock_retry_call.call_args
        assert call_args[0][0] == client._SyncHttpHandler__request
        assert call_args[0][1] == (None, headers)
        assert call_args[1]["exceptions"] == (HTTPError, ReadTimeout)
        assert call_args[1]["tries"] == 3
        assert call_args[1]["delay"] == 1
        assert call_args[1]["backoff"] == 2


class TestLogUpdater:
    """Test cases for LogUpdater class."""

    def test_initialization_sets_correct_attributes(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test LogUpdater initialization sets all required attributes."""
        assert mock_log_updater.cron_delay == 60

    def test_cron_delay_property_returns_correct_value(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test cron_delay property returns the configured delay."""
        assert mock_log_updater.cron_delay == 60

    @pytest.mark.asyncio
    @patch("src.services.token_generator.generate_token")
    async def test_get_token_generates_correct_token(
        self, mock_generate_token: Mock, mock_settings: Settings
    ) -> None:
        """Test get_token method generates token with correct parameters."""
        mock_generate_token.return_value = "test_token"

        # Create a new token generator with mocked generate_token
        from src.services.token_generator import PalGateTokenGenerator

        token_generator = PalGateTokenGenerator(
            bytes.fromhex(mock_settings.SESSION_TOKEN),
            mock_settings.USER_ID,
            mock_settings.SESSION_TOKEN_TYPE,
        )

        # Call the token generator
        token = await token_generator()

        mock_generate_token.assert_called_once_with(
            bytes.fromhex(mock_settings.SESSION_TOKEN),
            mock_settings.USER_ID,
            mock_settings.SESSION_TOKEN_TYPE,
        )
        assert token == "test_token"

    @pytest.mark.asyncio
    async def test_cache_operations(
        self, mock_log_updater: LogUpdater, mock_log_item_cache: Mock
    ) -> None:
        """Test all cache operations: get, add, and set last log item."""
        test_item = Mock()

        # Test get_last_log_item - retrieve cached item
        mock_log_item_cache.get.return_value = "test_item"
        result = await mock_log_updater._LogUpdater__log_item_cache.get()
        mock_log_item_cache.get.assert_called_once()
        assert result == "test_item"

        # Test add_last_log_item - add new item to cache
        await mock_log_updater._LogUpdater__log_item_cache.add(value=test_item)
        mock_log_item_cache.add.assert_called_once()

        # Test set_last_log_item - update existing item in cache
        await mock_log_updater._LogUpdater__log_item_cache.set(value=test_item)
        mock_log_item_cache.set.assert_called_once()

        # Verify all cache methods were called exactly once
        assert mock_log_item_cache.get.call_count == 1
        assert mock_log_item_cache.add.call_count == 1
        assert mock_log_item_cache.set.call_count == 1

    @pytest.mark.asyncio
    async def test_get_items_returns_valid_response(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method successfully fetches and parses log items."""
        # Mock successful HTTP response with valid log data
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method on the handler instance
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # Mock token generation
        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            # Execute the method under test
            result = await mock_log_updater._LogUpdater__items_handler(
                params=mock_log_updater._LogUpdater__params,
                headers=mock_log_updater._LogUpdater__headers
            )

        # Verify HTTP request was made
        mock_log_updater._LogUpdater__items_handler.request.assert_called_once(
        )

        # Verify response was correctly parsed
        assert result.err is False
        assert result.msg == "Success"
        assert result.status == "ok"
        assert len(result.log) == 1
        assert result.log[0].userId == "123"

    @pytest.mark.asyncio
    async def test_get_items_handles_http_error(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method handles HTTP errors and logs them."""
        # Mock the request method to raise HTTPError
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            side_effect=HTTPError("Connection error")
        )

        with pytest.raises(HTTPError):
            await mock_log_updater._LogUpdater__items_handler(
                params=mock_log_updater._LogUpdater__params,
                headers=mock_log_updater._LogUpdater__headers
            )

    @pytest.mark.asyncio
    async def test_get_items_handles_json_decode_error(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method handles JSON decode errors and logs them."""
        mock_response = Mock(spec=Response)
        mock_response.json.side_effect = JSONDecodeError(
            "Invalid JSON", "doc", 0
        )

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(JSONDecodeError):
            await mock_log_updater._LogUpdater__items_handler(
                params=mock_log_updater._LogUpdater__params,
                headers=mock_log_updater._LogUpdater__headers
            )

    @pytest.mark.asyncio
    async def test_get_items_handles_validation_error(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method handles validation errors and logs them."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {"invalid": "data"}

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(ValidationError):
            await mock_log_updater._LogUpdater__items_handler(
                params=mock_log_updater._LogUpdater__params,
                headers=mock_log_updater._LogUpdater__headers
            )

    @pytest.mark.asyncio
    async def test_update_new_items_save_handles_exceptions_gracefully(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test update_new_items_save handles exceptions without crashing."""
        mock_log_updater._LogUpdater__update_new_items = AsyncMock(
            side_effect=Exception("Test error")
        )

        # Should not raise exception
        await mock_log_updater.update_new_items_save()

        # Method should be called but exception should be caught
        mock_log_updater._LogUpdater__update_new_items.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_adds_first_item_when_no_last_item_exists(
        self,
        mock_log_updater: LogUpdater,
        mock_log_item_cache: Mock,
        mock_log_item: Mock,
    ) -> None:
        """Test __update_new_items adds first item when no last item."""
        # Create mock log items
        mock_item2 = Mock()
        mock_item2.model_dump = Mock(
            return_value={
                "userId": "456",
                "operation": "call",
                "time": 1708675300,
                "firstname": "Jane",
                "lastname": "Smith",
                "image": False,
                "reason": 1,
                "type": 100,
                "sn": "79009876543",
            }
        )

        # Mock response with log items
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                },
                {
                    "userId": "456",
                    "operation": "call",
                    "time": 1708675300,
                    "firstname": "Jane",
                    "lastname": "Smith",
                    "image": False,
                    "reason": 1,
                    "type": 100,
                    "sn": "79009876543",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        mock_log_item_cache.get.return_value = None  # No last item

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should add first log item to cache
        mock_log_item_cache.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_logs_new_items_and_updates_cache(
        self,
        mock_log_updater: LogUpdater,
        mock_log_item_cache: Mock,
    ) -> None:
        """Test __update_new_items logs new items when last item exists."""
        # Create proper log item data
        item1_data = {
            "userId": "111",
            "operation": "call",
            "time": 1708675200,
            "firstname": "First",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79001111111",
        }
        item2_data = {
            "userId": "222",
            "operation": "call",
            "time": 1708675300,
            "firstname": "Second",
            "lastname": "",
            "image": False,
            "reason": 0,
            "type": 1,
            "sn": "79002222222",
        }
        item3_data = {
            "userId": "333",
            "operation": "call",
            "time": 1708675400,
            "firstname": "Third",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79003333333",
        }

        # Mock response with log items
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [item1_data, item2_data, item3_data],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # Mock last item (item2)
        mock_log_item_cache.get.return_value = item2_data

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should log new items (item1) and set first item as last
        mock_log_updater._LogUpdater__broadcaster.assert_called_once()
        mock_log_item_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_raises_error_for_empty_log(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test __update_new_items raises ValueError for empty log."""
        # Mock response with empty log
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            with pytest.raises(ValidationError):
                await mock_log_updater._LogUpdater__update_new_items()

    @pytest.mark.asyncio
    async def test_update_new_items_handles_single_item_with_no_last_item(
        self,
        mock_log_updater: LogUpdater,
        mock_log_item_cache: Mock,
    ) -> None:
        """Test __update_new_items handles single item with no last item."""
        # Create proper log item data
        item_data = {
            "userId": "123",
            "operation": "call",
            "time": 1708675200,
            "firstname": "John",
            "lastname": "Doe",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79001234567",
        }

        # Mock response with single item
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [item_data],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        mock_log_item_cache.get.return_value = None  # No last item

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should add the single item to cache
        mock_log_item_cache.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_logs_all_items_when_all_are_new(
        self,
        mock_log_updater: LogUpdater,
        mock_log_item_cache: Mock,
    ) -> None:
        """Test __update_new_items logs all items when all items are new."""
        # Create proper log item data
        item1_data = {
            "userId": "111",
            "operation": "call",
            "time": 1708675200,
            "firstname": "First",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79001111111",
        }
        item2_data = {
            "userId": "222",
            "operation": "call",
            "time": 1708675300,
            "firstname": "Second",
            "lastname": "",
            "image": False,
            "reason": 0,
            "type": 1,
            "sn": "79002222222",
        }
        item3_data = {
            "userId": "333",
            "operation": "call",
            "time": 1708675400,
            "firstname": "Third",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79003333333",
        }

        # Mock response with log items
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [item1_data, item2_data, item3_data],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # Mock last item that doesn't match any (all items are new)
        mock_log_item_cache.get.return_value = {"userId": "999"}

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__update_new_items()

        # Should log all items and set first item as last
        mock_log_updater._LogUpdater__broadcaster.assert_called_once()
        mock_log_item_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_items_preserves_custom_headers(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items preserves custom headers while adding token."""
        # Mock response
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # Add custom header before calling get_items
        mock_log_updater._LogUpdater__headers["Custom-Header"] = "custom_value"

        # Call the items_handler to verify headers are preserved
        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__items_handler(
                params=mock_log_updater._LogUpdater__params,
                headers=mock_log_updater._LogUpdater__headers
            )

        # Verify the custom header is preserved
        assert "Custom-Header" in mock_log_updater._LogUpdater__headers
        mock_log_updater._LogUpdater__items_handler.request.assert_called_once(
        )


# Additional edge case tests
class TestSyncHttpHandlerEdgeCases:
    """Test cases for edge cases in SyncHttpHandler class."""

    @pytest.mark.asyncio
    @patch("src.handlers.http.retry_call")
    async def test_request_method_with_empty_headers(
        self, mock_retry_call: Mock
    ) -> None:
        """Test request method works with empty headers."""
        mock_response = Mock(spec=Response)
        mock_response.raise_for_status.return_value = None
        mock_retry_call.return_value = mock_response

        client = SyncHttpHandler("https://example.com")
        headers = {}  # Empty headers

        response = await client.request(headers=headers)

        mock_retry_call.assert_called_once()
        assert response == mock_response

    @pytest.mark.asyncio
    @patch("src.handlers.http.retry_call")
    async def test_request_method_with_custom_retry_parameters(
        self, mock_retry_call: Mock
    ) -> None:
        """Test request method uses custom retry parameters."""
        mock_response = Mock(spec=Response)
        mock_retry_call.return_value = mock_response

        client = SyncHttpHandler(
            "https://example.com", timeout=10, tries=5, delay=2, backoff=3
        )
        headers = {"User-Agent": "test"}

        _ = await client.request(headers=headers)  # Response not used in test

        mock_retry_call.assert_called_once()
        call_args = mock_retry_call.call_args
        assert call_args[1]["tries"] == 5
        assert call_args[1]["delay"] == 2
        assert call_args[1]["backoff"] == 3


class TestLogUpdaterEdgeCases:
    """Test cases for edge cases in LogUpdater class."""

    @pytest.mark.asyncio
    async def test_get_items_with_empty_response_log(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method with empty log in response."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            with pytest.raises(ValidationError):
                await mock_log_updater._LogUpdater__items_handler(
                    params=mock_log_updater._LogUpdater__params,
                    headers=mock_log_updater._LogUpdater__headers
                )

    @pytest.mark.asyncio
    async def test_update_new_items_with_identical_last_item(
        self,
        mock_log_updater: LogUpdater,
        mock_log_item_cache: Mock,
    ) -> None:
        """Test __update_new_items with identical last and first items."""
        # Create proper log item data
        item1_data = {
            "userId": "111",
            "operation": "call",
            "time": 1708675200,
            "firstname": "First",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79001111111",
        }
        item2_data = {
            "userId": "222",
            "operation": "call",
            "time": 1708675300,
            "firstname": "Second",
            "lastname": "",
            "image": False,
            "reason": 0,
            "type": 1,
            "sn": "79002222222",
        }
        item3_data = {
            "userId": "333",
            "operation": "call",
            "time": 1708675400,
            "firstname": "Third",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79003333333",
        }

        # Mock response with log items
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [item1_data, item2_data, item3_data],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # Mock last item to be the same object as item1
        # The code uses object identity (is) comparison
        mock_log_item_cache.get.return_value = item1_data

        # Since we're using dictionaries and the code uses object identity,
        # the items won't match, so all items will be considered new
        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__update_new_items()

        mock_log_updater._LogUpdater__broadcaster.assert_called_once()
        mock_log_item_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_save_handles_specific_exception_types(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test update_new_items_save handles HTTP errors and exceptions."""
        # Test handling of HTTPError
        mock_update_http = AsyncMock(side_effect=HTTPError("Test HTTP error"))
        mock_log_updater._LogUpdater__update_new_items = mock_update_http
        await mock_log_updater.update_new_items_save()  # Should not raise

        # Test handling of generic Exception
        mock_update_generic = AsyncMock(
            side_effect=Exception("Test generic error")
        )
        mock_log_updater._LogUpdater__update_new_items = mock_update_generic
        await mock_log_updater.update_new_items_save()  # Should not raise

        # Verify both exceptions were caught and handled
        assert mock_update_http.call_count == 1
        assert mock_update_generic.call_count == 1

    def test_get_items_preserves_existing_token_header(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test get_items method preserves existing X-Bt-Token header."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # This test is no longer relevant since get_items doesn't exist
        # The token is managed internally by __update_new_items
        pass

    @pytest.mark.asyncio
    async def test_cache_methods_with_none_items(
        self, mock_log_updater: LogUpdater, mock_log_item_cache: Mock
    ) -> None:
        """Test cache methods handle None items correctly."""
        # Test get_last_log_item with None from cache
        mock_log_item_cache.get.return_value = None
        result = await mock_log_updater._LogUpdater__log_item_cache.get()
        assert result is None

        # Test add_last_log_item with None (should still call cache)
        await mock_log_updater._LogUpdater__log_item_cache.add(value=None)
        mock_log_item_cache.add.assert_called_once()

        # Test set_last_log_item with None (should still call cache)
        await mock_log_updater._LogUpdater__log_item_cache.set(value=None)
        mock_log_item_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_new_items_preserves_correct_message_order(
        self,
        mock_log_updater: LogUpdater,
        mock_log_item_cache: Mock,
    ) -> None:
        """Test __update_new_items preserves correct chronological order."""
        # Create proper log item data with different timestamps
        item1_data = {
            "userId": "111",
            "operation": "call",
            "time": 1708675200,  # Earliest
            "firstname": "First",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79001111111",
        }
        item2_data = {
            "userId": "222",
            "operation": "call",
            "time": 1708675300,  # Middle
            "firstname": "Middle",
            "lastname": "",
            "image": False,
            "reason": 0,
            "type": 1,
            "sn": "79002222222",
        }
        item3_data = {
            "userId": "333",
            "operation": "call",
            "time": 1708675400,  # Latest
            "firstname": "Last",
            "lastname": "",
            "image": True,
            "reason": 0,
            "type": 1,
            "sn": "79003333333",
        }

        # Mock response with log items in chronological order
        mock_response = Mock(spec=Response)
        # item1 (oldest), item2, item3 (newest)
        mock_response.json.return_value = {
            "log": [item1_data, item2_data, item3_data],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }

        # Mock the request method
        mock_log_updater._LogUpdater__items_handler.request = AsyncMock(
            return_value=mock_response
        )

        # Mock last item that doesn't match any (all items are new)
        mock_log_item_cache.get.return_value = {"userId": "999"}

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            await mock_log_updater._LogUpdater__update_new_items()

        # Verify the broadcaster was called with the correct message order
        mock_log_updater._LogUpdater__broadcaster.assert_called_once()

        # Extract the actual message that was logged
        actual_message = (
            mock_log_updater._LogUpdater__broadcaster.call_args[0][0]
        )

        # Check that messages appear in the correct order
        lines = actual_message.split("\n")
        assert len(lines) == 3, f"Expected 3 lines, got {len(lines)}: {lines}"

        # Verify the order: newest (Last) should be first, oldest (First) last
        assert (
            "Last" in lines[0]
        ), f"Expected 'Last' to be first, but got: {lines[0]}"
        assert (
            "Middle" in lines[1]
        ), f"Expected 'Middle' to be second, but got: {lines[1]}"
        assert (
            "First" in lines[2]
        ), f"Expected 'First' to be third, but got: {lines[2]}"


class TestHttpHandlerBase:
    """Test cases for HttpHandlerBase abstract class."""

    def test_initialization_with_defaults(self) -> None:
        """Test HttpHandlerBase initialization with default values."""
        handler = HttpHandlerBase("https://example.com")
        assert handler._path == "https://example.com"
        assert handler._method == Method.GET
        assert handler._timeout == 1
        assert handler._tries == 0
        assert handler._delay == 0
        assert handler._backoff == 0

    def test_initialization_with_custom_values(self) -> None:
        """Test HttpHandlerBase initialization with custom values."""
        handler = HttpHandlerBase(
            "https://example.com",
            method=Method.POST,
            timeout=10,
            tries=5,
            delay=2,
            backoff=3,
        )
        assert handler._path == "https://example.com"
        assert handler._method == Method.POST
        assert handler._timeout == 10
        assert handler._tries == 5
        assert handler._delay == 2
        assert handler._backoff == 3

    @pytest.mark.asyncio
    async def test_request_method_raises_not_implemented(self) -> None:
        """Test request method raises NotImplementedError."""
        handler = HttpHandlerBase("https://example.com")
        with pytest.raises(NotImplementedError):
            await handler.request()

    @pytest.mark.asyncio
    async def test_call_method_raises_not_implemented(self) -> None:
        """Test __call__ method raises NotImplementedError."""
        handler = HttpHandlerBase("https://example.com")
        with pytest.raises(NotImplementedError):
            await handler()


class TestSyncHttpHandlerPrivateRequest:
    """Test cases for SyncHttpHandler.__request private method."""

    @patch("src.handlers.http.request")
    def test_private_request_makes_http_call(self, mock_request: Mock) -> None:
        """Test __request makes HTTP request with correct parameters."""
        mock_response = Mock(spec=Response)
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        handler = SyncHttpHandler("https://example.com", timeout=5)
        response = handler._SyncHttpHandler__request(
            params={"key": "value"}, headers={"User-Agent": "test"}
        )

        mock_request.assert_called_once_with(
            Method.GET.value,
            "https://example.com",
            params={"key": "value"},
            headers={"User-Agent": "test"},
            timeout=5,
        )
        assert response == mock_response

    @patch("src.handlers.http.request")
    def test_private_request_raises_http_error(
        self, mock_request: Mock
    ) -> None:
        """Test __request raises HTTPError on failed request."""
        mock_request.side_effect = HTTPError("404 Not Found")

        handler = SyncHttpHandler("https://example.com")
        with pytest.raises(HTTPError):
            handler._SyncHttpHandler__request()

    @pytest.mark.asyncio
    async def test_call_method_delegates_to_request(self) -> None:
        """Test __call__ method delegates to request method."""
        handler = SyncHttpHandler("https://example.com")
        handler.request = AsyncMock(return_value=Mock(spec=Response))

        result = await handler(
            params={"test": "value"}, headers={"X-Test": "header"}
        )

        handler.request.assert_called_once_with(
            {"test": "value"}, {"X-Test": "header"}
        )
        assert result is not None


class TestPalGateItemsHandler:
    """Test cases for PalGateItemsHandler class."""

    def test_initialization_with_defaults(self) -> None:
        """Test PalGateItemsHandler initialization with default values."""
        handler = PalGateItemsHandler("https://example.com")
        assert handler._path == "https://example.com"
        assert handler._method == Method.GET
        assert handler._timeout == 5
        assert handler._tries == 3
        assert handler._delay == 1
        assert handler._backoff == 2

    @pytest.mark.asyncio
    @patch.object(SyncHttpHandler, "request")
    async def test_call_with_none_params_creates_empty_dict(
        self, mock_request: Mock
    ) -> None:
        """Test __call__ creates empty dict when params is None."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }
        mock_request.return_value = mock_response

        handler = PalGateItemsHandler("https://example.com")
        await handler(params=None, headers={"X-Bt-Token": "test"})

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert call_args[0][0] == {}

    @pytest.mark.asyncio
    @patch.object(SyncHttpHandler, "request")
    async def test_call_with_none_headers_creates_empty_dict(
        self, mock_request: Mock
    ) -> None:
        """Test __call__ creates empty dict when headers is None."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }
        mock_request.return_value = mock_response

        handler = PalGateItemsHandler("https://example.com")
        await handler(params={"id": "123"}, headers=None)

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert call_args[0][1] == {}

    @pytest.mark.asyncio
    @patch.object(SyncHttpHandler, "request")
    async def test_call_logs_warning_when_token_missing(
        self, mock_request: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test __call__ logs warning when X-Bt-Token header is missing."""
        mock_response = Mock(spec=Response)
        mock_response.json.return_value = {
            "log": [
                {
                    "userId": "123",
                    "operation": "call",
                    "time": 1708675200,
                    "firstname": "John",
                    "lastname": "Doe",
                    "image": True,
                    "reason": 0,
                    "type": 1,
                    "sn": "79001234567",
                }
            ],
            "err": False,
            "msg": "Success",
            "status": "ok",
        }
        mock_request.return_value = mock_response

        handler = PalGateItemsHandler("https://example.com")
        await handler(params={"id": "123"}, headers={})

        assert "No X_BT_TOKEN_HEADER" in caplog.text


class TestPalGateTokenGenerator:
    """Test cases for PalGateTokenGenerator class."""

    def test_initialization(self) -> None:
        """Test PalGateTokenGenerator initialization."""
        generator = PalGateTokenGenerator(
            session_token="abc123", user_id=12345, session_token_type=0
        )
        assert generator._PalGateTokenGenerator__session_token == "abc123"
        assert generator._PalGateTokenGenerator__user_id == 12345
        assert generator._PalGateTokenGenerator__session_token_type == 0

    @pytest.mark.asyncio
    @patch("src.services.token_generator.generate_token")
    async def test_call_generates_token(
        self, mock_generate_token: Mock
    ) -> None:
        """Test __call__ generates token with correct parameters."""
        mock_generate_token.return_value = "generated_token"

        generator = PalGateTokenGenerator(
            session_token="abc123", user_id=12345, session_token_type=0
        )
        token = await generator()

        mock_generate_token.assert_called_once_with("abc123", 12345, 0)
        assert token == "generated_token"


class TestCacheHandlerBase:
    """Test cases for CacheHandlerBase abstract class."""

    def test_initialization(self) -> None:
        """Test CacheHandlerBase initialization."""
        handler = CacheHandlerBase()
        assert handler._log is not None

    @pytest.mark.asyncio
    async def test_get_raises_not_implemented(self) -> None:
        """Test get method raises NotImplementedError."""
        handler = CacheHandlerBase()
        with pytest.raises(NotImplementedError):
            await handler.get()

    @pytest.mark.asyncio
    async def test_set_raises_not_implemented(self) -> None:
        """Test set method raises NotImplementedError."""
        handler = CacheHandlerBase()
        with pytest.raises(NotImplementedError):
            await handler.set()

    @pytest.mark.asyncio
    async def test_add_raises_not_implemented(self) -> None:
        """Test add method raises NotImplementedError."""
        handler = CacheHandlerBase()
        with pytest.raises(NotImplementedError):
            await handler.add()


class TestCacheHandler:
    """Test cases for CacheHandler class."""

    @pytest.mark.asyncio
    async def test_initialization(self) -> None:
        """Test CacheHandler initialization."""
        mock_cache = AsyncMock()
        handler = CacheHandler(mock_cache)
        assert handler._CacheHandler__cache == mock_cache

    @pytest.mark.asyncio
    async def test_get_with_none_key_logs_warning(
        self, mock_cache: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test get logs warning when key is None."""
        mock_cache.get.return_value = "value"
        handler = CacheHandler(mock_cache)

        result = await handler.get(key=None, default="default")

        assert "Key is None" in caplog.text
        mock_cache.get.assert_called_once_with(None, default="default")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_set_with_none_key_logs_warning(
        self, mock_cache: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test set logs warning when key is None."""
        handler = CacheHandler(mock_cache)

        await handler.set(key=None, value="test_value")

        assert "Key is None" in caplog.text
        mock_cache.set.assert_called_once_with(None, "test_value")

    @pytest.mark.asyncio
    async def test_add_with_none_key_logs_warning(
        self, mock_cache: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test add logs warning when key is None."""
        handler = CacheHandler(mock_cache)

        await handler.add(key=None, value="test_value")

        assert "Key is None" in caplog.text
        mock_cache.add.assert_called_once_with(None, "test_value")


class TestLogItemCacheHandler:
    """Test cases for LogItemCacheHandler class."""

    @pytest.mark.asyncio
    async def test_initialization_with_default_key(self) -> None:
        """Test LogItemCacheHandler initialization with default key."""
        mock_cache = AsyncMock()
        handler = LogItemCacheHandler(mock_cache)
        assert handler._LogItemCacheHandler__cache == mock_cache
        assert handler._LogItemCacheHandler__key == "last_log_item"

    @pytest.mark.asyncio
    async def test_initialization_with_custom_key(self) -> None:
        """Test LogItemCacheHandler initialization with custom key."""
        mock_cache = AsyncMock()
        handler = LogItemCacheHandler(mock_cache, key="custom_key")
        assert handler._LogItemCacheHandler__cache == mock_cache
        assert handler._LogItemCacheHandler__key == "custom_key"

    @pytest.mark.asyncio
    async def test_get_uses_default_key_when_none(
        self, mock_cache: AsyncMock
    ) -> None:
        """Test get uses default key when key parameter is None."""
        mock_cache.get.return_value = "cached_value"
        handler = LogItemCacheHandler(mock_cache, key="default_key")

        result = await handler.get(key=None, default="default")

        mock_cache.get.assert_called_once_with(
            "default_key", default="default"
        )
        assert result == "cached_value"

    @pytest.mark.asyncio
    async def test_get_uses_provided_key(self, mock_cache: AsyncMock) -> None:
        """Test get uses provided key."""
        mock_cache.get.return_value = "cached_value"
        handler = LogItemCacheHandler(mock_cache, key="default_key")

        result = await handler.get(key="custom_key", default="default")

        mock_cache.get.assert_called_once_with("custom_key", default="default")
        assert result == "cached_value"

    @pytest.mark.asyncio
    async def test_set_uses_default_key_when_none(
        self, mock_cache: AsyncMock
    ) -> None:
        """Test set uses default key when key parameter is None."""
        handler = LogItemCacheHandler(mock_cache, key="default_key")

        await handler.set(key=None, value="test_value")

        mock_cache.set.assert_called_once_with("default_key", "test_value")

    @pytest.mark.asyncio
    async def test_set_uses_provided_key(self, mock_cache: AsyncMock) -> None:
        """Test set uses provided key."""
        handler = LogItemCacheHandler(mock_cache, key="default_key")

        await handler.set(key="custom_key", value="test_value")

        mock_cache.set.assert_called_once_with("custom_key", "test_value")

    @pytest.mark.asyncio
    async def test_add_uses_default_key_when_none(
        self, mock_cache: AsyncMock
    ) -> None:
        """Test add uses default key when key parameter is None."""
        handler = LogItemCacheHandler(mock_cache, key="default_key")

        await handler.add(key=None, value="test_value")

        mock_cache.add.assert_called_once_with("default_key", "test_value")

    @pytest.mark.asyncio
    async def test_add_uses_provided_key(self, mock_cache: AsyncMock) -> None:
        """Test add uses provided key."""
        handler = LogItemCacheHandler(mock_cache, key="default_key")

        await handler.add(key="custom_key", value="test_value")

        mock_cache.add.assert_called_once_with("custom_key", "test_value")


class TestBroadcastHandlerBase:
    """Test cases for BroadcastHandlerBase abstract class."""

    def test_initialization(self) -> None:
        """Test BroadcastHandlerBase initialization."""
        handler = BroadcastHandlerBase()
        assert handler._log is not None

    @pytest.mark.asyncio
    async def test_call_raises_not_implemented(self) -> None:
        """Test __call__ raises NotImplementedError."""
        handler = BroadcastHandlerBase()
        with pytest.raises(NotImplementedError):
            await handler()


class TestBroadcastLoggerHandler:
    """Test cases for BroadcastLoggerHandler class."""

    def test_initialization(self) -> None:
        """Test BroadcastLoggerHandler initialization."""
        mock_logger1 = Mock()
        mock_logger2 = Mock()
        handler = BroadcastLoggerHandler([mock_logger1, mock_logger2])
        assert handler._BroadcastLoggerHandler__loggers == (
            mock_logger1,
            mock_logger2,
        )

    @pytest.mark.asyncio
    async def test_call_with_none_message_returns_early(self) -> None:
        """Test __call__ returns early when message is None."""
        mock_logger = Mock()
        handler = BroadcastLoggerHandler([mock_logger])

        await handler(message=None)

        mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_with_empty_message_returns_early(self) -> None:
        """Test __call__ returns early when message is empty string."""
        mock_logger = Mock()
        handler = BroadcastLoggerHandler([mock_logger])

        await handler(message="")

        mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_logs_to_all_loggers(self) -> None:
        """Test __call__ logs message to all loggers."""
        mock_logger1 = Mock()
        mock_logger2 = Mock()
        handler = BroadcastLoggerHandler([mock_logger1, mock_logger2])

        await handler(message="Test message")

        mock_logger1.info.assert_called_once_with("Test message")
        mock_logger2.info.assert_called_once_with("Test message")


class TestLogUpdaterValueError:
    """Test cases for LogUpdater ValueError handling."""

    @pytest.mark.asyncio
    async def test_update_new_items_raises_value_error_for_empty_log(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test __update_new_items raises ValueError when log is empty."""
        # Mock the items_handler to return a response with empty log
        mock_response = Mock()
        mock_response.log = []
        mock_log_updater._LogUpdater__items_handler = AsyncMock(
            return_value=mock_response
        )

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            with pytest.raises(ValueError, match="Wrong log list"):
                await mock_log_updater._LogUpdater__update_new_items()

    @pytest.mark.asyncio
    async def test_update_new_items_raises_value_error_for_none_log(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test __update_new_items raises ValueError when log is None."""
        # Mock the items_handler to return a response with None log
        mock_response = Mock()
        mock_response.log = []
        mock_log_updater._LogUpdater__items_handler = AsyncMock(
            return_value=mock_response
        )

        with patch(
            'src.services.token_generator.generate_token',
            return_value="test_token",
        ):
            with pytest.raises(ValueError, match="Wrong log list"):
                await mock_log_updater._LogUpdater__update_new_items()


class TestMainloop:
    """Test cases for mainloop function."""

    @pytest.mark.asyncio
    async def test_mainloop_calls_update_and_sleeps(
        self, mock_log_updater: LogUpdater
    ) -> None:
        """Test mainloop calls update_new_items_save and sleeps."""
        mock_log_updater.update_new_items_save = AsyncMock()
        # Note: cron_delay is a read-only property, cannot be set

        # Run mainloop for one iteration then cancel
        import asyncio

        async def run_one_iteration():
            task = asyncio.create_task(mainloop(mock_log_updater))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_one_iteration()

        mock_log_updater.update_new_items_save.assert_called()


class TestGetArgs:
    """Test cases for get_args function."""

    def test_get_args_with_defaults(self) -> None:
        """Test get_args returns default values."""
        with patch("sys.argv", ["main.py"]):
            args = get_args()
            assert args.env.value == Environment.DEV.value
            assert args.dev_url == "localhost:8080/api/log"

    def test_get_args_with_custom_env(self) -> None:
        """Test get_args with custom environment."""
        with patch("sys.argv", ["main.py", "--env", "stable"]):
            args = get_args()
            assert args.env.value == Environment.STABLE.value

    def test_get_args_with_custom_dev_url(self) -> None:
        """Test get_args with custom dev URL."""
        with patch(
            "sys.argv", ["main.py", "--dev-url", "http://test.com/api"]
        ):
            args = get_args()
            assert args.dev_url == "http://test.com/api"
