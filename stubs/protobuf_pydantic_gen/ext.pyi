from typing import Type, TypeVar

from google.protobuf import message as _message
from google.protobuf.descriptor_pool import DescriptorPool as _DescriptorPool
from google.protobuf.descriptor import Descriptor as _Descriptor
from pydantic import BaseModel
# from sqlmodel import SQLModel


PydanticModel = TypeVar("PydanticModel", bound="BaseModel")


class Descriptor(_Descriptor): ...


class DescriptorPool(_DescriptorPool):
    def FindMessageTypeByName(self, full_name: str) -> Descriptor: ...


pool: DescriptorPool


def model2protobuf(model: PydanticModel, proto: _message.Message) -> _message.Message: ...

def protobuf2model(model_cls: Type[PydanticModel], proto: _message.Message) -> PydanticModel: ...
