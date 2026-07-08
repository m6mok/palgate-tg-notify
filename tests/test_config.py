import pytest
from pydantic import ValidationError

from config import Settings


class TestSettingsValidation:
    def test_valid_settings_pass(self, settings: Settings) -> None:
        assert settings.DEVICE_ID == "test_device"
        assert settings.CRON_DELAY == 60

    def test_session_token_must_be_hex(self, settings: Settings) -> None:
        with pytest.raises(ValidationError, match="hex"):
            Settings(**{**settings.model_dump(), "SESSION_TOKEN": "not-hex!"})

    def test_url_must_contain_device_placeholder(
        self, settings: Settings
    ) -> None:
        with pytest.raises(ValidationError, match="device_id"):
            Settings(
                **{
                    **settings.model_dump(),
                    "URL_USER_LOG": "https://example.com/log",
                }
            )

    def test_negative_cron_delay_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(ValidationError):
            Settings(**{**settings.model_dump(), "CRON_DELAY": -1})

    def test_alert_threshold_must_be_positive(
        self, settings: Settings
    ) -> None:
        with pytest.raises(ValidationError):
            Settings(**{**settings.model_dump(), "ALERT_AFTER_FAILURES": 0})

    def test_session_token_bytes_decodes_hex(self, settings: Settings) -> None:
        assert settings.session_token_bytes == bytes.fromhex(
            settings.SESSION_TOKEN
        )

    def test_resilience_knobs_have_defaults(self, settings: Settings) -> None:
        data = settings.model_dump()
        for field in (
            "STATE_FILE",
            "HEARTBEAT_FILE",
            "LOCK_TIMEOUT",
            "MAX_BACKOFF",
            "ALERT_AFTER_FAILURES",
        ):
            data.pop(field)

        defaults = Settings(**data)

        assert defaults.STATE_FILE == "data/state.json"
        assert defaults.HEARTBEAT_FILE == "data/heartbeat"
        assert defaults.LOCK_TIMEOUT == 60
        assert defaults.MAX_BACKOFF == 300
        assert defaults.ALERT_AFTER_FAILURES == 10

    def test_max_channel_is_optional_and_off_by_default(
        self, settings: Settings
    ) -> None:
        assert settings.MAX_API_TOKEN == ""
        assert settings.MAX_CHAT_ID == 0
