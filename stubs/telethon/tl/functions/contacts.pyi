from typing import Any


class ImportContactsRequest:
    def __init__(self, contacts: list[Any]) -> None: ...


class DeleteContactsRequest:
    def __init__(self, id: list[Any]) -> None: ...


class AddContactRequest:
    def __init__(
        self,
        id: Any,
        first_name: str,
        last_name: str,
        phone: str,
        add_phone_privacy_exception: bool = ...,
    ) -> None: ...
