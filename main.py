from datetime import datetime, timezone, timedelta
from enum import Enum
from os import environ
from time import sleep as time_sleep
from typing import Callable, Iterable

from pylgate import generate_token
from requests import get as requests_get, post as requests_post
from schedule import every as schedule_every, run_pending as schedule_run_pending


TELEGRAM_API_SEND_MESSAGE_URL: str = "https://api.telegram.org/bot{token}/sendMessage"


class LogType(Enum):
    CALL = 1
    ADMIN = 100


class LogItem:
    tz: timezone | None = None
    last: "LogItem"

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
        return " ".join(
            (
                str(self.timestamp),
                f"{self.sn if self.userId == "0" else self.userId:12}",
                f"{self.type.name:5}",
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


class Notify:
    send: Callable[[str], None]
    log: Callable[[str], None]


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


def gen_until_eq(target: LogItem, items: Iterable[LogItem]):
    for item in items:
        if item != target:
            yield item
        else:
            break


def get_items(
    url: str,
    token_fabric: Callable[[], str],
    headers: dict = {"User-Agent": "okhttp/4.9.3"},
) -> tuple[LogItem]:
    headers["X-Bt-Token"] = token_fabric()
    response = requests_get(url, headers=headers)

    data: dict[str, str | list[dict]] = response.json()
    if not response.ok or data.get("err", False) or data.get("status", "") != "ok":
        Notify.log(f"error: {data}")
        return tuple()

    log = data.get("log")
    if log is None or not isinstance(log, list):
        Notify.log(f"error: {data}")
        return tuple()

    return tuple(LogItem(item) for item in log)


def tg_send_message(
    token: str, chat_id: int, retries: int = 5
) -> Callable[[str], None]:
    def _tg_send_message(text: str) -> None:
        retry = retries
        url = TELEGRAM_API_SEND_MESSAGE_URL.format(token=token)
        while (retry := retry - 1) >= 0:
            response = requests_post(url, data={"chat_id": chat_id, "text": text})
            if response.status_code == 200:
                break
            print("Retry send message")
            time_sleep(5)

    return _tg_send_message


def job(url: str, token_fabric: Callable[[], str]) -> None:
    items: list[LogItem] = get_items(url, token_fabric)
    Notify.log(f"{len(items)}\n" + "\n\n".join(str(item) for item in items))
    if len(items) == 0 or items[0] == LogItem.last:
        return

    new_items = tuple(gen_until_eq())
    LogItem.last = new_items[0]

    Notify.send("\n\n".join(str(item) for item in new_items))


def main(
    device_id: str,
    user_id: int,
    session_token: bytes,
    session_token_type: int,
    url_user_log: str,
    tz: timezone,
    telegram_api_token: str,
    telegram_chat_id: int,
    telegram_log_chat_id: int,
    cron_delay: int,
) -> None:
    url: str = url_user_log.format(device_id=device_id)
    token_fabric: Callable[[], str] = token(session_token, user_id, session_token_type)

    LogItem.tz = tz

    Notify.send = tg_send_message(telegram_api_token, telegram_chat_id)
    Notify.log = tg_send_message(telegram_api_token, telegram_log_chat_id)

    Notify.log(f"Program started {user_id=} {device_id=} {cron_delay=}")

    if len(items := get_items(url, token_fabric)) == 0:
        return
    else:
        LogItem.last = items[0]

    schedule_every(cron_delay).seconds.do(lambda: job(url, token_fabric))

    while 1:
        schedule_run_pending()
        time_sleep(1)


if __name__ == "__main__":
    main(
        _getenv("DEVICE_ID"),
        int(_getenv("USER_ID")),
        bytes.fromhex(_getenv("SESSION_TOKEN")),
        int(_getenv("SESSION_TOKEN_TYPE")),
        _getenv("URL_USER_LOG"),
        timezone(timedelta(hours=int(_getenv("TZ")))),
        _getenv("TELEGRAM_API_TOKEN"),
        int(_getenv("TELEGRAM_CHAT_ID")),
        int(_getenv("TELEGRAM_LOG_CHAT_ID")),
        int(_getenv("CRON_DELAY")),
    )
