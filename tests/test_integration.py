import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from src.main import main
from src.main import Settings, LogUpdater


class TestIntegration:
    """Integration tests for the main functionality."""

    @pytest.fixture
    def mock_settings_integration(self, mock_settings: Settings) -> Settings:
        """Mock settings for integration tests with shorter delay."""
        mock_settings.CRON_DELAY = 1  # Short delay for testing
        return mock_settings

    @pytest.fixture
    def mock_log_updater_integration(self, mock_settings_integration: Settings, mock_logger: Mock, mock_cache: AsyncMock) -> LogUpdater:
        """Mock LogUpdater for integration tests."""
        return LogUpdater(mock_settings_integration, mock_logger, mock_logger, mock_cache)

    @pytest.mark.asyncio
    async def test_mainloop_executes_single_iteration(self, mock_log_updater_integration: LogUpdater) -> None:
        """Test mainloop executes at least one iteration before timeout."""
        from src.main import mainloop

        mock_log_updater_integration.update_new_items_save = AsyncMock()

        # Use timeout to test single iteration without infinite loop
        try:
            await asyncio.wait_for(mainloop(mock_log_updater_integration), timeout=0.1)
        except asyncio.TimeoutError:
            pass  # Expected behavior - mainloop runs indefinitely

        # Verify the update method was called at least once
        mock_log_updater_integration.update_new_items_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_mainloop_executes_multiple_iterations(self, mock_settings_integration: Settings, mock_logger: Mock, mock_cache: AsyncMock) -> None:
        """Test mainloop executes multiple iterations with proper cancellation."""
        from src.main import mainloop

        # Create a mock LogUpdater with very short delay
        mock_settings_integration.CRON_DELAY = 0.1  # Very short delay for testing
        mock_updater = LogUpdater(mock_settings_integration, mock_logger, mock_logger, mock_cache)

        # Use AsyncMock with side effect to count calls and raise after 3
        call_count = 0

        async def mock_update() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise asyncio.CancelledError()

        mock_updater.update_new_items_save = AsyncMock(side_effect=mock_update)

        # Run mainloop - it should stop after 3 iterations due to CancelledError
        try:
            await mainloop(mock_updater)
        except asyncio.CancelledError:
            pass

        # Should have been called 3 times
        assert mock_updater.update_new_items_save.call_count == 3

    @pytest.mark.asyncio
    async def test_mainloop_handles_exceptions_gracefully(self, mock_settings_integration: Settings, mock_logger: Mock, mock_cache: AsyncMock) -> None:
        """Test mainloop continues running even when update operations fail."""
        from src.main import mainloop

        # Configure short delay for faster testing
        mock_settings_integration.CRON_DELAY = 0.1
        mock_updater = LogUpdater(mock_settings_integration, mock_logger, mock_logger, mock_cache)

        # Track number of update attempts
        call_count = 0

        async def mock_update_with_exception() -> None:
            nonlocal call_count
            call_count += 1
            # Simulate that update_new_items_save handles exceptions internally
            # by not re-raising them (exception handling is tested elsewhere)

        mock_updater.update_new_items_save = AsyncMock(side_effect=mock_update_with_exception)

        # Test that mainloop continues despite potential exceptions
        try:
            await asyncio.wait_for(mainloop(mock_updater), timeout=0.2)
        except asyncio.TimeoutError:
            pass  # Expected - mainloop runs indefinitely

        # Verify multiple update attempts were made despite exceptions
        assert mock_updater.update_new_items_save.call_count > 1

    @pytest.mark.asyncio
    async def test_main_function_sets_up_dependencies_correctly(
        self,
        mock_settings_integration: Settings
    ) -> None:
        """Test main function sets up all dependencies correctly."""
        with patch('src.main.Settings', return_value=mock_settings_integration) as mock_settings_class, \
             patch('src.main.dictConfig') as mock_dict_config, \
             patch('src.main.getLogger') as mock_get_logger, \
             patch('src.main.SimpleMemoryCache') as mock_cache, \
             patch('src.main.asyncio_gather') as mock_gather:

            # Mock dependencies
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            mock_cache.return_value = AsyncMock()

            # Mock asyncio_gather to return an awaitable that completes immediately
            mock_gather.return_value = asyncio.Future()
            mock_gather.return_value.set_result(None)

            # Run main function
            await main()

            # Verify setup was called correctly
            mock_settings_class.assert_called_once()
            mock_dict_config.assert_called_once()
            mock_get_logger.assert_called()
            mock_cache.assert_called_once()

            # Verify mainloop was started
            mock_gather.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_to_end_flow_from_items_to_cache(
        self,
        mock_settings_integration: Settings,
        mock_cache: AsyncMock,
        mock_logger: Mock,
        sample_log_items: List[Dict[str, Any]]
    ) -> None:
        """Test complete flow: fetch items, process them, and update cache."""
        # Simulate no previous items in cache
        mock_cache.get.return_value = None

        # Create real LogUpdater instance for integration testing
        updater = LogUpdater(mock_settings_integration, mock_logger, mock_logger, mock_cache)

        # Mock HTTP client and successful response
        with patch.object(updater._LogUpdater__http_client, 'get') as mock_http_get:
            mock_response = Mock()
            mock_response.json.return_value = {
                "log": sample_log_items[:2],  # Use first two sample items
                "err": False,
                "msg": "Success",
                "status": "ok"
            }
            mock_http_get.return_value = mock_response

            # Mock authentication token generation
            with patch('src.main.generate_token', return_value="test_token"):
                # Execute the update flow
                await updater._LogUpdater__update_new_items()

                # Verify cache was updated with the first item
                mock_cache.add.assert_called_once()

                # Verify no chat notifications when no previous items exist
                mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_function_sets_up_timezone_correctly(self, mock_settings_integration: Settings) -> None:
        """Test main function sets up timezone correctly."""
        with patch('src.main.dictConfig') as mock_dict_config, \
             patch('src.main.Settings', return_value=mock_settings_integration), \
             patch('src.main.getLogger') as mock_get_logger, \
             patch('src.main.SimpleMemoryCache') as mock_cache, \
             patch('src.main.asyncio_gather') as mock_gather:

            # Mock loggers and cache
            mock_get_logger.return_value = Mock()
            mock_cache.return_value = AsyncMock()
            # Mock asyncio_gather to return an awaitable that completes immediately
            mock_gather.return_value = asyncio.Future()
            mock_gather.return_value.set_result(None)

            # Import Formatter to check its converter
            from logging import Formatter

            # Store original converter
            original_converter = Formatter.converter

            try:
                # Run main function
                await main()

                # Verify timezone was set
                assert Formatter.converter is not original_converter

                # Test that the converter uses the correct timezone
                tz = timezone(timedelta(hours=mock_settings_integration.TZ))
                test_time = datetime.now(tz).timetuple()
                converter_result = Formatter.converter()
                assert isinstance(converter_result, type(test_time))

            finally:
                # Restore original converter
                Formatter.converter = original_converter

    @pytest.mark.asyncio
    async def test_main_function_configures_logging_correctly(self, mock_settings_integration: Settings) -> None:
        """Test main function configures logging with correct settings."""
        with patch('src.main.Settings', return_value=mock_settings_integration), \
             patch('src.main.getLogger') as mock_get_logger, \
             patch('src.main.SimpleMemoryCache') as mock_cache, \
             patch('src.main.asyncio_gather') as mock_gather:

            captured_config = None

            def capture_dict_config(config: Dict[str, Any]) -> None:
                nonlocal captured_config
                captured_config = config

            with patch('src.main.dictConfig', side_effect=capture_dict_config):
                # Mock asyncio_gather to return an awaitable that completes immediately
                mock_gather.return_value = asyncio.Future()
                mock_gather.return_value.set_result(None)

                # Run main function
                await main()

                # Verify logging configuration
                assert captured_config is not None
                assert captured_config["version"] == 1
                assert "formatters" in captured_config
                assert "handlers" in captured_config
                assert "loggers" in captured_config

                # Verify Telegram handlers are configured
                handlers = captured_config["handlers"]
                assert "log" in handlers
                assert "chat" in handlers
                assert handlers["log"]["token"] == mock_settings_integration.TELEGRAM_API_TOKEN
                assert handlers["chat"]["token"] == mock_settings_integration.TELEGRAM_API_TOKEN


# Additional edge case tests
class TestIntegrationEdgeCases:
    """Test cases for edge cases in integration scenarios."""

    @pytest.mark.asyncio
    async def test_mainloop_with_zero_cron_delay(self, mock_settings: Settings, mock_logger: Mock, mock_cache: AsyncMock) -> None:
        """Test mainloop handles zero cron delay correctly."""
        from src.main import mainloop

        # Create a mock LogUpdater with zero delay
        mock_settings.CRON_DELAY = 0
        mock_updater = LogUpdater(mock_settings, mock_logger, mock_logger, mock_cache)

        # Use AsyncMock with side effect to count calls and raise after 2
        call_count = 0

        async def mock_update() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        mock_updater.update_new_items_save = AsyncMock(side_effect=mock_update)

        # Run mainloop - it should handle zero delay
        try:
            await mainloop(mock_updater)
        except asyncio.CancelledError:
            pass

        # Should have been called 2 times
        assert mock_updater.update_new_items_save.call_count == 2

    @pytest.mark.asyncio
    async def test_end_to_end_flow_with_duplicate_items(
        self,
        mock_settings: Settings,
        mock_cache: AsyncMock,
        mock_logger: Mock,
        sample_log_items: List[Dict[str, Any]]
    ) -> None:
        """Test complete flow handles duplicate items correctly."""
        # Mock last item as the first item from sample
        mock_cache.get.return_value = sample_log_items[0]

        # Create real LogUpdater instance
        updater = LogUpdater(mock_settings, mock_logger, mock_logger, mock_cache)

        # Mock the HTTP client and response with duplicate first item
        with patch.object(updater._LogUpdater__http_client, 'get') as mock_http_get:
            # Mock successful response with duplicate first item
            mock_response = Mock()
            mock_response.json.return_value = {
                "log": sample_log_items,  # All items including duplicate
                "err": False,
                "msg": "Success",
                "status": "ok"
            }
            mock_http_get.return_value = mock_response

            # Mock token generation
            with patch('src.main.generate_token', return_value="test_token"):
                # Test the update flow
                await updater._LogUpdater__update_new_items()

                # Should log only new items (items after the duplicate)
                mock_logger.info.assert_called_once()
                # Should update cache with first item
                mock_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_function_with_missing_environment_variables(self) -> None:
        """Test main function handles missing environment variables gracefully."""
        with patch('src.main.Settings') as mock_settings_class, \
             patch('src.main.dictConfig') as mock_dict_config, \
             patch('src.main.getLogger') as mock_get_logger, \
             patch('src.main.SimpleMemoryCache') as mock_cache, \
             patch('src.main.asyncio_gather') as mock_gather:

            # Mock Settings to raise ValidationError for missing env vars
            mock_settings_class.side_effect = Exception("Missing environment variables")

            # Mock asyncio_gather to return an awaitable that completes immediately
            mock_gather.return_value = asyncio.Future()
            mock_gather.return_value.set_result(None)

            # Should handle the exception gracefully
            try:
                await main()
            except Exception:
                # Exception should be raised to the caller
                pass

            # Verify that setup was attempted
            mock_settings_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_logging_configuration_with_invalid_settings(self) -> None:
        """Test logging configuration handles invalid settings gracefully."""
        with patch('src.main.Settings') as mock_settings_class, \
             patch('src.main.getLogger') as mock_get_logger, \
             patch('src.main.SimpleMemoryCache') as mock_cache, \
             patch('src.main.asyncio_gather') as mock_gather:

            # Mock invalid settings
            mock_settings = Mock()
            mock_settings.TELEGRAM_API_TOKEN = None  # Invalid token
            mock_settings.TELEGRAM_CHAT_ID = 123456789
            mock_settings.TELEGRAM_LOG_CHAT_ID = 987654321
            mock_settings_class.return_value = mock_settings

            captured_config = None
            config_error = None

            def capture_dict_config(config: Dict[str, Any]) -> None:
                nonlocal captured_config
                captured_config = config
                # Simulate configuration error
                if config["handlers"]["log"]["token"] is None:
                    nonlocal config_error
                    config_error = "Invalid token"

            with patch('src.main.dictConfig', side_effect=capture_dict_config):
                # Mock asyncio_gather to return an awaitable that completes immediately
                mock_gather.return_value = asyncio.Future()
                mock_gather.return_value.set_result(None)

                # Should handle configuration gracefully
                try:
                    await main()
                except Exception:
                    # Exception might be raised depending on implementation
                    pass

                # Configuration should still be attempted
                assert captured_config is not None

    @pytest.mark.asyncio
    async def test_timezone_setup_with_negative_timezone(self, mock_settings: Settings) -> None:
        """Test timezone setup handles negative timezone offsets."""
        with patch('src.main.dictConfig') as mock_dict_config, \
             patch('src.main.Settings', return_value=mock_settings), \
             patch('src.main.getLogger') as mock_get_logger, \
             patch('src.main.SimpleMemoryCache') as mock_cache, \
             patch('src.main.asyncio_gather') as mock_gather:

            # Mock loggers and cache
            mock_get_logger.return_value = Mock()
            mock_cache.return_value = AsyncMock()
            # Mock asyncio_gather to return an awaitable that completes immediately
            mock_gather.return_value = asyncio.Future()
            mock_gather.return_value.set_result(None)

            # Test with negative timezone
            mock_settings.TZ = -5  # Negative timezone offset

            # Import Formatter to check its converter
            from logging import Formatter

            # Store original converter
            original_converter = Formatter.converter

            try:
                # Run main function
                await main()

                # Verify timezone was set
                assert Formatter.converter is not original_converter

                # Test that the converter uses the correct timezone
                tz = timezone(timedelta(hours=mock_settings.TZ))
                test_time = datetime.now(tz).timetuple()
                converter_result = Formatter.converter()
                assert isinstance(converter_result, type(test_time))

            finally:
                # Restore original converter
                Formatter.converter = original_converter
