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

LOG_NO_REASON_SIGN = "❌"

LOG_VALUE_TYPE = Union[str, int, bool]


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

    @property
    def pn(self) -> str:
        result: str = self.sn if self.userId == "0" else self.userId

        if (length := len(result)) == 9:
            result = "79" + result
        elif length < 9:
            result = "79" + "0" * (9 - length) + result

        return result


class LogMessage(object):
    last_item: Union[LogItem, None] = None

    def __init__(self, data: Dict[str, LOG_VALUE_TYPE]) -> None:
        self.__item = LogItem(**data)

        self.fullname: str = " ".join(
            name for name in (self.__item.firstname, self.__item.lastname) if name != ""
        )
        self.pn = self.__item.pn

    def is_item(self, item: LogItem) -> bool:
        return self.__item == item

    def is_last_item(self) -> bool:
        target: Union[LogItem, None] = LogMessage.last_item

        if target is None:
            return False

        return self.is_item(target)

    def get_type_sign(self) -> Union[str, None]:
        return LOG_TYPE_SIGN.get(self.__item.type.value, None)

    def get_reason(self) -> Union[str, None]:
        return LOG_NO_REASON_SIGN if self.__item.reason != 0 else None

    def set_as_last(self) -> None:
        LogMessage.last_item = self.__item

    def __str__(self) -> str:
        return " ".join(
            field
            for field in (
                self.fullname if self.fullname != "Unknown" else "?",
                f'<a href="+{self.pn}">{self.pn}</a>',
                self.get_type_sign(),
                self.get_reason(),
            )
            if field is not None
        )


class API(object):
    url: Union[str, None] = None
    token_fabric: Union[Callable[[], str], None] = None

    @staticmethod
    def init(
        url: str, session_token: bytes, user_id: int, session_token_type: TokenType
    ) -> None:
        API.url = url
        API.token_fabric = API.token(session_token, user_id, session_token_type)

    @staticmethod
    def token(
        session_token: bytes, user_id: int, session_token_type: TokenType
    ) -> Callable[[], str]:
        def _token() -> str:
            return generate_token(session_token, user_id, session_token_type)

        return _token

    @staticmethod
    def upload_gen(
        headers: Dict[str, str] = {"User-Agent": "okhttp/4.9.3"},
    ) -> Generator[LogMessage, Any, None]:
        if API.url is None or API.token_fabric is None:
            raise SyntaxError("need to init the API before upload_gen")

        headers["X-Bt-Token"] = API.token_fabric()
        response = requests_get(API.url, headers=headers)

        if response.status_code != 200:
            Telegram.log(f"error {response.status_code=}\n{response.text[:500]=}")
            return
        elif not (content_type := response.headers["Content-Type"]).startswith(
            "application/json"
        ):
            Telegram.log(f"error {content_type=}")
            return

        data: Dict[str, Union[LOG_VALUE_TYPE, List[Dict[str, LOG_VALUE_TYPE]]]] = (
            response.json()
        )
        if (
            not response.ok
            or data.get("err", True) is True
            or data.get("status", "") != "ok"
        ):
            Telegram.log(f"error: {data}")
            return

        log = data.get("log")
        if log is None or not isinstance(log, List):
            Telegram.log(f"error: {data}")
            return

        for item in log:
            yield LogMessage(item)

    @staticmethod
    def select_up_to_last_gen(
        messages: Iterable[LogMessage],
    ) -> Generator[LogMessage, Any, None]:
        target_item = LogMessage.last_item

        if target_item is None:
            yield from messages
            return

        for message in messages:
            if not message.is_item(target_item):
                yield message
            else:
                break

    @staticmethod
    def cache_warming() -> None:
        messages_gen: Generator[LogMessage, Any, None] = API.upload_gen()
        try:
            next(messages_gen).set_as_last()
        except StopIteration:
            Telegram.log("No messages while uploading")
        except Exception as e:
            Telegram.log("fatal error")
            raise e


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


def job() -> None:
    try:
        __job()
    except Exception as e:
        Telegram.log("fatal error")
        raise e


def __job() -> None:
    text_messages: List[str] = []

    new_messages_gen: Generator[LogMessage, Any, None] = API.select_up_to_last_gen(
        API.upload_gen()
    )
    try:
        message: LogMessage = next(new_messages_gen)
        if message.is_last_item():
            return
        message.set_as_last()
        text_messages.append(str(message))
    except StopIteration:
        pass

    for message in new_messages_gen:
        text_messages.append(str(message))

    if len(text_messages) > 0:
        Telegram.send("\n".join(text_messages))


def main(
    device_id: str,
    user_id: int,
    session_token: bytes,
    session_token_type: TokenType,
    url_user_log: str,
    telegram_api_token: str,
    telegram_chat_id: int,
    telegram_log_chat_id: int,
    cron_delay: int,
) -> None:
    API.init(
        url_user_log.format(device_id=device_id),
        session_token,
        user_id,
        session_token_type,
    )

    Telegram.send = Telegram.send_message_fabric(telegram_api_token, telegram_chat_id)
    Telegram.log = Telegram.send_message_fabric(
        telegram_api_token, telegram_log_chat_id
    )

    Telegram.log(f"Program started {user_id=} {device_id=} {cron_delay=}")

    API.cache_warming()

    schedule_every(cron_delay).seconds.do(job)

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
        _getenv("TELEGRAM_API_TOKEN"),
        int(_getenv("TELEGRAM_CHAT_ID")),
        int(_getenv("TELEGRAM_LOG_CHAT_ID")),
        int(_getenv("CRON_DELAY")),
    )
