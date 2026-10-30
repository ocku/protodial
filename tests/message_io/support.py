import io
from types import SimpleNamespace
from typing import Any

from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    message_factory,
)


def _message_class() -> Any:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="messages_test.proto", package="messages_test", syntax="proto3"
    )
    message = file_proto.message_type.add(name="Payload")
    message.field.add(
        name="display_name",
        json_name="displayName",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    message.field.add(
        name="count",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    )
    message.field.add(
        name="data",
        number=3,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return message_factory.GetMessageClass(
        pool.FindMessageTypeByName("messages_test.Payload")
    ), pool


MESSAGE, POOL = _message_class()


def _oneof_message_class() -> tuple[Any, descriptor_pool.DescriptorPool]:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="raw_oneof_test.proto",
        package="raw_oneof_test",
        syntax="proto3",
    )
    message = file_proto.message_type.add(name="Payload")
    message.oneof_decl.add(name="content")
    message.field.add(
        name="data",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
        oneof_index=0,
    )
    message.field.add(
        name="reference",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        oneof_index=0,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return (
        message_factory.GetMessageClass(
            pool.FindMessageTypeByName("raw_oneof_test.Payload")
        ),
        pool,
    )


ONEOF_MESSAGE, ONEOF_POOL = _oneof_message_class()


def _options(**overrides: Any) -> SimpleNamespace:
    values = {
        "allow_unknown_fields": False,
        "emit_defaults": False,
        "max_input_buffer": None,
        "max_send_message_size": None,
        "output_format": "json",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ChunkSource(io.BytesIO):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class Read1Source:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        raise AssertionError("incremental input must use read1")

    def read1(self, _size: int = -1) -> bytes:
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""
