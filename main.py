from datetime import datetime, timezone, timedelta
from enum import Enum
from os import environ
from time import sleep as time_sleep
from typing import Any, Callable, Dict, Generator, Iterable, List, Union

from pydantic import BaseModel
from pylgate import generate_token  # type: ignore[attr-defined]
from pylgate.types import TokenType
from requests import get as requests_get, post as requests_post
from schedule import every as schedule_every, run_pending as schedule_run_pending


LOG_TYPE_SIGN = {1: "📞", 100: "📱"}

LOG_VALUE_TYPE = Union[str, int, bool]


def get_pn(sn: str, userId: str) -> str:
    result: str = sn if userId == "0" else userId

    if (length := len(result)) == 9:
        result = "79" + result
    elif length < 9:
        result = "79" + "0" * (9 - length) + result

    return result


class LogItemType(Enum):
    UNDEFINED = 0
    CALL = 1
    ADMIN = 100


class LogItem(BaseModel):
    userId: str = "0"
    operation: str = ""
    time: int = 0
    firstname: str = ""
    lastname: str = ""
    image: bool = False
    reason: int = 0
    type: LogItemType = LogItemType.UNDEFINED
    sn: str = ""


class Message(object):
    tz: Union[timezone, None] = None
    last_item: Union[LogItem, None] = None

    def __init__(self, data: Dict[str, LOG_VALUE_TYPE]) -> None:
        self.item = LogItem(**data)

        self.timestamp: datetime = datetime.fromtimestamp(self.item.time, self.tz)
        self.fullname: str = " ".join(
            name
            for name in (self.item.firstname, self.item.lastname)
            if name is not None and name != ""
        )
        self.type_sign: str = LOG_TYPE_SIGN[self.item.type.value]
        self.pn = get_pn(self.item.sn, self.item.userId)

    def __str__(self) -> str:
        return " ".join(
            (
                self.fullname if self.fullname != "Unknown" else "?",
                f'<a href="+{self.pn}">{self.pn}</a>',
                f"{self.type_sign:5}",
            )
        )


class Telegram:
    send: Callable[[str], None]
    log: Callable[[str], None]

    API_BASE = "https://api.telegram.org/"
    API_SEND_MESSAGE = "sendMessage"

    @staticmethod
    def send_message_url(token: str) -> str:
        return Telegram.API_BASE + f"bot{token}/" + Telegram.API_SEND_MESSAGE

    @staticmethod
    def send_message_fabric(
        token: str, chat_id: int, retries: int = 5
    ) -> Callable[[str], None]:
        url: str = Telegram.send_message_url(token)
        textless_data: Dict[str, Union[str, int]] = dict(
            chat_id=chat_id, parse_mode="HTML"
        )

        def fabric(text: str) -> None:
            current_retry = retries
            data = textless_data.copy()
            data["text"] = text
            while (current_retry := current_retry - 1) >= 0:
                response = requests_post(url, data=data)
                if response.status_code == 200:
                    break
                print("Retry send message")
                time_sleep(5)

        return fabric


def _getenv(key: str, default: Union[str, None] = None) -> str:
    if (result := environ.get(key, default)) is None:
        raise ValueError(f"No env param `{key}`")
    else:
        return result


def token(
    session_token: bytes, user_id: int, session_token_type: TokenType
) -> Callable[[], str]:
    def _token() -> str:
        return generate_token(session_token, user_id, session_token_type)

    return _token


def gen_until_last(messages: Iterable[Message]) -> Generator[Message, Any, None]:
    target_item = Message.last_item
    for message in messages:
        if message.item != target_item:
            yield message
        else:
            break


def get_items(
    url: str,
    token_fabric: Callable[[], str],
    headers: Dict[str, str] = {"User-Agent": "okhttp/4.9.3"},
) -> tuple[Message, ...]:
    headers["X-Bt-Token"] = token_fabric()
    response = requests_get(url, headers=headers)

    if response.status_code != 200:
        Telegram.log(f"error {response.status_code=}\n{response.text[:500]=}")
        return tuple()

    if not (content_type := response.headers["Content-Type"]).startswith(
        "application/json"
    ):
        Telegram.log(f"error {content_type=}")
        return tuple()

    data: Dict[str, Union[LOG_VALUE_TYPE, List[Dict[str, LOG_VALUE_TYPE]]]] = (
        response.json()
    )
    if not response.ok or data.get("err", False) or data.get("status", "") != "ok":
        Telegram.log(f"error: {data}")
        return tuple()

    log = data.get("log")
    if log is None or not isinstance(log, List):
        Telegram.log(f"error: {data}")
        return tuple()

    return tuple(Message(item) for item in log)


def job(url: str, token_fabric: Callable[[], str]) -> None:
    try:
        __job(url, token_fabric)
    except Exception as e:
        Telegram.log(f"fatel error {e}")
        raise e


def __job(url: str, token_fabric: Callable[[], str]) -> None:
    messages: tuple[Message, ...] = get_items(url, token_fabric)
    if len(messages) == 0 or messages[0].item == Message.last_item:
        return

    new_messages = tuple(gen_until_last(messages))
    Message.last_item = new_messages[0].item

    Telegram.send("\n\n".join(str(message) for message in new_messages))


def main(
    device_id: str,
    user_id: int,
    session_token: bytes,
    session_token_type: TokenType,
    url_user_log: str,
    tz: timezone,
    telegram_api_token: str,
    telegram_chat_id: int,
    telegram_log_chat_id: int,
    cron_delay: int,
) -> None:
    url: str = url_user_log.format(device_id=device_id)
    token_fabric: Callable[[], str] = token(session_token, user_id, session_token_type)

    Message.tz = tz

    Telegram.send = Telegram.send_message_fabric(telegram_api_token, telegram_chat_id)
    Telegram.log = Telegram.send_message_fabric(
        telegram_api_token, telegram_log_chat_id
    )

    Telegram.log(f"Program started {user_id=} {device_id=} {cron_delay=}")

    try:
        if len(messages := get_items(url, token_fabric)) == 0:
            return
        else:
            Message.last_item = messages[0].item
    except Exception as e:
        Telegram.log(f"error {e}")
        raise e

    schedule_every(cron_delay).seconds.do(lambda: job(url, token_fabric))

    while 1:
        schedule_run_pending()
        time_sleep(1)


if __name__ == "__main__":
    main(
        _getenv("DEVICE_ID"),
        int(_getenv("USER_ID")),
        bytes.fromhex(_getenv("SESSION_TOKEN")),
        TokenType(int(_getenv("SESSION_TOKEN_TYPE"))),
        _getenv("URL_USER_LOG"),
        timezone(timedelta(hours=int(_getenv("TZ")))),
        _getenv("TELEGRAM_API_TOKEN"),
        int(_getenv("TELEGRAM_CHAT_ID")),
        int(_getenv("TELEGRAM_LOG_CHAT_ID")),
        int(_getenv("CRON_DELAY")),
    )
