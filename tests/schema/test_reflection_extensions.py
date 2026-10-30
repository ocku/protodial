from __future__ import annotations

from typing import Any

from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial.schema.reflection import (
    ReflectionDescriptorSource,
)

from .support import _Call, _echo_request, _grpc


def test_reflection_discovers_extension_file_and_all_numbers() -> None:
    base = descriptor_pb2.FileDescriptorProto(
        name="base.proto", package="edge", syntax="proto2"
    )
    message = base.message_type.add(name="Base")
    message.extension_range.add(start=100, end=200)

    extensions = descriptor_pb2.FileDescriptorProto(
        name="extensions.proto", package="edge", syntax="proto2"
    )
    extensions.dependency.append("base.proto")
    extensions.extension.add(
        name="marker",
        number=123,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".edge.Base",
    )
    requests: list[str] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                requests.append(kind)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.SetInParent()
                elif kind == "file_containing_symbol":
                    response.file_descriptor_response.file_descriptor_proto.append(
                        base.SerializeToString()
                    )
                elif kind == "file_containing_extension":
                    response.file_descriptor_response.file_descriptor_proto.extend(
                        (
                            extensions.SerializeToString(),
                            base.SerializeToString(),
                        )
                    )
                elif kind == "all_extension_numbers_of_type":
                    response.all_extension_numbers_response.base_type_name = (
                        "edge.Base"
                    )
                    response.all_extension_numbers_response.extension_number.append(
                        123
                    )
                else:
                    raise AssertionError(kind)
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    source = ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())
    base_descriptor = source.pool.FindMessageTypeByName("edge.Base")

    extension = source.pool.FindExtensionByNumber(base_descriptor, 123)
    all_extensions = source.pool.FindAllExtensions(base_descriptor)

    assert extension.full_name == "edge.marker"
    assert [item.number for item in all_extensions] == [123]
    assert "file_containing_extension" in requests
    assert "all_extension_numbers_of_type" in requests
