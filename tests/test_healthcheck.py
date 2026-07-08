from pathlib import Path

from healthcheck import is_alive


NOW = 1_751_500_000.0


class TestIsAlive:
    def test_future_deadline_is_healthy(self, tmp_path: Path) -> None:
        heartbeat = tmp_path / "heartbeat"
        heartbeat.write_text(str(NOW + 120))

        assert is_alive(heartbeat, NOW) is True

    def test_expired_deadline_is_unhealthy(self, tmp_path: Path) -> None:
        heartbeat = tmp_path / "heartbeat"
        heartbeat.write_text(str(NOW - 1))

        assert is_alive(heartbeat, NOW) is False

    def test_missing_file_is_unhealthy(self, tmp_path: Path) -> None:
        assert is_alive(tmp_path / "missing", NOW) is False

    def test_garbage_content_is_unhealthy(self, tmp_path: Path) -> None:
        heartbeat = tmp_path / "heartbeat"
        heartbeat.write_text("not a number")

        assert is_alive(heartbeat, NOW) is False
