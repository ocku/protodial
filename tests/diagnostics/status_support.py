from __future__ import annotations

from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
)

from protodial.rich_status import decoding as status_decoding


def _custom_pool_and_payload() -> tuple[descriptor_pool.DescriptorPool, bytes]:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="example/detail.proto",
        package="example",
        syntax="proto3",
    )
    message = file_proto.message_type.add(name="ValidationDetail")
    message.field.add(
        name="reason",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("example.ValidationDetail")
    detail = status_decoding.message_class(descriptor)(reason="bad name")
    return pool, detail.SerializeToString()


def _status_bytes(
    *details: tuple[str, bytes], code: int = 3, message: str = "invalid"
) -> bytes:
    status = status_decoding.status_message_class()(code=code, message=message)
    for type_url, payload in details:
        packed = status.details.add()
        packed.type_url = type_url
        packed.value = payload
    return status.SerializeToString()


def _decoded_detail(message: object) -> status_decoding.DecodedStatusDetail:
    descriptor = message.DESCRIPTOR
    return status_decoding.DecodedStatusDetail(
        f"type.googleapis.com/{descriptor.full_name}",
        descriptor.full_name,
        message.SerializeToString(),
        message=message,
    )
