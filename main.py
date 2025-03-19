from datetime import datetime, timezone, timedelta
from os import environ
from os.path import (
    exists as os_path_exists,
    join as os_path_join,
    dirname as os_path_dirname,
)
from typing import Callable, Iterable
from enum import Enum

from pylgate import generate_token
import requests
from dotenv import load_dotenv


if os_path_exists(path := os_path_join(os_path_dirname(__file__), ".env")):
    load_dotenv(path)


class LogType(Enum):
    CALL = 1
    ADMIN = 100


class LogItem:
    tz: timezone | None = None

    def __init__(self, data: dict) -> None:
        self.userId: str = data.get("userId", "<>")
        self.operation: str = data.get("operation", "")
        self.time: int = data.get("time", 0)
        self.firstname: str = data.get("firstname", "")
        self.lastname: str = data.get("lastname", "")
        self.image: bool = data.get("image", False)
        self.reason: int = data.get("reason", 0)
        self.type: LogType = LogType(data.get("type", 0))
        self.sn: str = data.get("sn", "")

        self.timestamp: datetime = datetime.fromtimestamp(self.time, self.tz)
        self.fullname: str = " ".join(
            name
            for name in (self.firstname, self.lastname)
            if name is not None and name != ""
        )

    def __str__(self) -> str:
        return "\t".join(
            (
                str(self.timestamp),
                self.sn if self.userId == "0" else self.userId,
                self.type.name,
                self.fullname if self.fullname != "Unknown" else "?",
            )
        )

    def __eq__(self, other) -> bool:
        if other is None:
            return False
        elif isinstance(other, LogItem):
            return all(
                (
                    self.userId == other.userId,
                    self.operation == other.operation,
                    self.time == other.time,
                    self.type == self.type,
                    self.sn == other.sn,
                )
            )
        return NotImplemented


def _getenv(key: str, default=None) -> str:
    if (result := environ.get(key, default)) is None:
        raise ValueError(f"No env param `{key}`")
    else:
        return result


def token(
    session_token: bytes, user_id: int, session_token_type: int
) -> Callable[[], str]:
    def _token() -> str:
        return generate_token(session_token, user_id, session_token_type)

    return _token


def gen_until_eq(target: LogItem | None, items: Iterable[LogItem]):
    if target is None:
        return

    for item in items:
        if item != target:
            yield item


def get_new_items(
    url: str,
    token_fabric: Callable[[], str],
    last_item: LogItem | None = None,
    headers: dict = {"User-Agent": "okhttp/4.9.3"},
) -> tuple[LogItem]:
    headers["X-Bt-Token"] = token_fabric()
    response = requests.get(url, headers=headers)

    data: dict[str, str | list[dict]] = response.json()
    if not response.ok or data.get("err", False) or data.get("status", "") != "ok":
        print(f"error: {data}")
        return []

    log = data.get("log")
    if log is None or not isinstance(log, list):
        print(f"error: {data}")
        return []

    return tuple(gen_until_eq(last_item, (LogItem(item) for item in log)))


def main(
    device_id: str,
    user_id: int,
    session_token: bytes,
    session_token_type: int,
    url_user_log: str,
    tz: timezone,
) -> None:
    print('Program started')

    url: str = url_user_log.format(device_id=device_id)
    token_fabric: Callable[[], str] = token(session_token, user_id, session_token_type)
    last_item: LogItem | None = None

    LogItem.tz = tz

    if len(items := get_new_items(url, token_fabric, last_item)) > 1:
        last_item = items[0]


if __name__ == "__main__":
    main(
        _getenv("DEVICE_ID"),
        int(_getenv("USER_ID")),
        bytes.fromhex(_getenv("SESSION_TOKEN")),
        int(_getenv("SESSION_TOKEN_TYPE")),
        _getenv("URL_USER_LOG"),
        timezone(timedelta(hours=int(_getenv("TZ")))),
    )
