from asyncio import sleep as asyncio_sleep

from pylgate.token_generator import generate_token
from pylgate.types import TokenType


class PalGateTokenGenerator:
    def __init__(
        self,
        session_token: bytes,
        user_id: int,
        session_token_type: TokenType,
    ) -> None:
        self.__session_token = session_token
        self.__user_id = user_id
        self.__session_token_type = session_token_type

    async def __call__(self) -> str:
        await asyncio_sleep(0)
        return generate_token(
            self.__session_token,
            self.__user_id,
            self.__session_token_type,
        )
