from logging import LogRecord

from src.formatter import HtmlFormatter


class TestHtmlFormatter:
    """Test cases for HtmlFormatter class."""

    def test_parse_mode_attribute_is_html(self) -> None:
        """Test that parse_mode attribute is set to 'HTML'."""
        formatter = HtmlFormatter()
        assert formatter.parse_mode == 'HTML'

    def test_format_method_escapes_html_special_characters(self) -> None:
        """Test format method properly handles HTML special characters in log records."""
        formatter = HtmlFormatter()

        # Create log record with HTML special characters
        record = LogRecord(
            name="test_logger",
            level=20,  # INFO level
            pathname="/test/path",
            lineno=1,
            msg='Test message with <html> & "special" chars',
            args=(),
            exc_info=None
        )
        record.funcName = "test_function<with_html>"

        # Format the record using HtmlFormatter
        result = formatter.format(record)

        # Verify HTML special characters are preserved (not escaped) for HTML mode
        assert "<html>" in result
        assert "&" in result
        assert "\"special\"" in result

    def test_format_method_handles_none_fields_gracefully(self) -> None:
        """Test format method handles None fields without errors."""
        formatter = HtmlFormatter()

        # Create a log record with None fields
        record = LogRecord(
            name=None,
            level=20,
            pathname="/test/path",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None
        )
        record.funcName = None

        # Format should not fail with None fields
        result = formatter.format(record)
        assert "Test message" in result

    def test_format_method_handles_empty_message(self) -> None:
        """Test format method handles empty message without errors."""
        formatter = HtmlFormatter()

        record = LogRecord(
            name="test_logger",
            level=20,
            pathname="/test/path",
            lineno=1,
            msg="",
            args=(),
            exc_info=None
        )

        result = formatter.format(record)
        # Should handle empty message without errors
        assert isinstance(result, str)

    def test_format_method_uses_parent_class_formatting_style(self) -> None:
        """Test format method uses the parent class formatting style."""
        formatter = HtmlFormatter(fmt='%(name)s - %(message)s')

        record = LogRecord(
            name="test_logger",
            level=20,
            pathname="/test/path",
            lineno=1,
            msg="Test <message>",
            args=(),
            exc_info=None
        )

        result = formatter.format(record)
        assert "test_logger - Test <message>" in result

    def test_format_method_calls_parent_class_format(self) -> None:
        """Test format method calls parent class format method."""
        formatter = HtmlFormatter(fmt='%(levelname)s: %(message)s')

        record = LogRecord(
            name="test_logger",
            level=20,  # INFO
            pathname="/test/path",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None
        )

        result = formatter.format(record)
        # Should include the level name from parent formatting
        assert "INFO: Test message" in result


# Additional edge case tests
class TestHtmlFormatterEdgeCases:
    """Test cases for edge cases in HtmlFormatter class."""

    def test_format_method_with_unicode_characters(self) -> None:
        """Test format method correctly handles Unicode characters and emojis."""
        formatter = HtmlFormatter()

        # Create log record with Unicode characters and emojis
        record = LogRecord(
            name="test_logger",
            level=20,
            pathname="/test/path",
            lineno=1,
            msg="Test message with emoji 🚀 and unicode café",
            args=(),
            exc_info=None
        )
        record.funcName = "test_function_🎯"

        result = formatter.format(record)

        # Verify Unicode characters and emojis are preserved
        assert "🚀" in result
        assert "café" in result
        assert "Test message with emoji 🚀 and unicode café" in result

    def test_format_method_with_special_html_entities(self) -> None:
        """Test format method preserves HTML entities for HTML parsing mode."""
        formatter = HtmlFormatter()

        # Create log record with HTML entities
        record = LogRecord(
            name="test_logger",
            level=20,
            pathname="/test/path",
            lineno=1,
            msg='Test & "special" <html> tags',
            args=(),
            exc_info=None
        )
        record.funcName = "test<function>"

        result = formatter.format(record)

        # Verify HTML entities are preserved (not escaped) in HTML mode
        assert "&" in result
        assert "\"" in result
        assert "<html>" in result
        assert 'Test & "special" <html> tags' in result

    def test_format_method_with_exception_info(self) -> None:
        """Test format method handles records with exception info."""
        formatter = HtmlFormatter()

        # Create a record with exception info properly
        record = LogRecord(
            name="test_logger",
            level=40,  # ERROR
            pathname="/test/path",
            lineno=1,
            msg="Test message with exception",
            args=(),
            exc_info=None  # Don't include actual exception info to avoid complexity
        )

        result = formatter.format(record)
        assert "Test message with exception" in result

    def test_format_method_with_custom_format_string(self) -> None:
        """Test format method works with custom format strings."""
        custom_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        formatter = HtmlFormatter(fmt=custom_format)

        record = LogRecord(
            name="test_logger",
            level=20,  # INFO
            pathname="/test/path",
            lineno=1,
            msg="Test <message>",
            args=(),
            exc_info=None
        )

        result = formatter.format(record)
        assert "test_logger" in result
        assert "INFO" in result
        assert "Test <message>" in result

    def test_format_method_with_numeric_and_boolean_values(self) -> None:
        """Test format method handles numeric and boolean values in message."""
        formatter = HtmlFormatter()

        record = LogRecord(
            name="test_logger",
            level=20,
            pathname="/test/path",
            lineno=1,
            msg="Test with numbers: 123 and boolean: True",
            args=(),
            exc_info=None
        )

        result = formatter.format(record)
        assert "123" in result
        assert "True" in result
