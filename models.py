from enum import Enum
from typing import Self

from pydantic import BaseModel, model_validator


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

    @property
    def fullname(self) -> str:
        return " ".join(name for name in (self.firstname, self.lastname) if name != "")

    @property
    def type_sign(self) -> str | None:
        match self.type:
            case LogItemType.UNDEFINED:
                return None
            case LogItemType.CALL:
                return "📞"
            case LogItemType.ADMIN:
                return "📱"

    @property
    def reason_sign(self) -> str | None:
        if self.reason == 0:
            return None  # "✅"
        return "❌"

    def __str__(self) -> str:
        fullname = self.fullname
        return " ".join(
            field
            for field in (
                fullname if fullname != "Unknown" else "?",
                f'<a href="+{self.pn}">{self.pn}</a>',
                self.type_sign,
                self.reason_sign,
            )
            if field is not None
        )


class LogItemResponse(BaseModel):
    log: list[LogItem]
    err: bool | None
    msg: str
    status: str

    @model_validator(mode="after")
    def correct_response_match(self) -> Self:
        if self.status != "ok":
            raise ValueError("Status is not `ok`: %s" % self.status)
        elif self.err is not None and self.err:
            raise ValueError("Error catched, status: %s" % self.status)
        elif len(self.log) == 0:
            raise ValueError("There is no log elements, status: %s" % self.status)
        return self
