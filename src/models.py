from typing import Self

from pydantic import model_validator, field_validator

from log_item_model import LogItem as _LogItem, LogItemType, LogItemResponse


LogItem = _LogItem


class Item(_LogItem):
    @staticmethod
    def from_log_item(log_item: _LogItem) -> "Item":
        return Item(**log_item.model_dump())

    @property
    def pn(self) -> str:
        result: str | None = (
            self.sn
            if (self.sn is not None and self.sn != "") or self.userId is None or self.userId == "0"
            else self.userId
        )
        if result is None:
            raise ValueError("Phone numer field is None")

        if (length := len(result)) == 9:
            result = "79" + result
        elif length < 9:
            result = "79" + "0" * (9 - length) + result

        return result

    @property
    def fullname(self) -> str:
        return " ".join(name for name in (self.firstname, self.lastname) if name is not None and name != "")

    @property
    def type_sign(self) -> str | None:
        match self.type:
            case LogItemType.UNDEFINED:
                return None
            case LogItemType.CALL:
                return "📞"
            case LogItemType.ADMIN:
                return "📱"
        return None

    @property
    def reason_sign(self) -> str | None:
        if self.reason == 0:
            return None  # "✅"
        return "❌"

    def __str__(self) -> str:
        fullname = self.fullname
        pn = self.pn
        return " ".join(
            field
            for field in (
                fullname if fullname != "Unknown" else "?",
                f'<a href="+{pn}">{pn}</a>',
                self.type_sign,
                self.reason_sign,
            )
            if field is not None
        )

    def __repr__(self) -> str:
        return self.pn


class ItemResponse(LogItemResponse):
    @field_validator("log", mode="before")
    @classmethod
    def define_optional_fields(cls, log: list[dict[str, str]]) -> list[dict[str, str]]:
        for log_item in log:
            log_item.setdefault("lastname", "")

        return log

    @model_validator(mode="after")
    def correct_response_match(self) -> Self:
        if self.status != "ok":
            raise ValueError("Status is not `ok`: %s" % self.status)
        elif self.err is not None and self.err:
            raise ValueError("Error catched, status: %s" % self.status)
        elif self.log is None or len(self.log) == 0:
            raise ValueError("There is no log elements, status: %s" % self.status)
        return self
