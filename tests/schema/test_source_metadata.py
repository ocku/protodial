from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool
from grpc_reflection.v1alpha import reflection_pb2

from protodial.presentation.descriptor import render_descriptor
from protodial.schema.reflection import ReflectionDescriptorSource
from protodial.schema.source_protos import (
    field_proto,
    register_file_proto,
    register_pool_file_protos,
)

from .support import _Call, _echo_request, _RpcError, _StatusCode


def test_public_reflection_pool_preserves_source_metadata_for_lazy_files() -> (
    None
):
    docs_proto = descriptor_pb2.FileDescriptorProto(
        name="docs.proto", package="docs", syntax="proto2"
    )
    message_proto = docs_proto.message_type.add(name="Message")
    value_proto = message_proto.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        default_value="from source",
    )
    message_proto.extension_range.add(start=100, end=101)
    docs_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" reflected message\n"
    )
    docs_proto.source_code_info.location.add(
        path=(4, 0, 2, 0), leading_comments=" reflected field\n"
    )

    other_proto = descriptor_pb2.FileDescriptorProto(
        name="other.proto", package="other", syntax="proto3"
    )
    other_proto.message_type.add(name="Message")
    other_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" reflected file route\n"
    )

    extension_proto = descriptor_pb2.FileDescriptorProto(
        name="extension.proto",
        package="extra",
        syntax="proto2",
        dependency=("docs.proto",),
    )
    extension_proto.extension.add(
        name="note",
        number=100,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".docs.Message",
        default_value="extension source",
    )
    extension_proto.source_code_info.location.add(
        path=(7, 0), leading_comments=" reflected extension\n"
    )
    request_kinds: list[str] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                request_kinds.append(kind)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.SetInParent()
                elif kind == "file_containing_symbol":
                    symbol = request.file_containing_symbol
                    selected = (
                        docs_proto if symbol == "docs.Message" else other_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                elif kind == "file_containing_extension":
                    response.file_descriptor_response.file_descriptor_proto.extend(
                        (
                            extension_proto.SerializeToString(),
                            docs_proto.SerializeToString(),
                        )
                    )
                else:
                    raise AssertionError(kind)
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    message = source.pool.FindMessageTypeByName("docs.Message")
    assert render_descriptor(message).startswith(
        "// reflected message\nmessage docs.Message {"
    )
    assert "// reflected field" in render_descriptor(message)
    stored_value = source.file_protos["docs.proto"].message_type[0].field[0]
    assert field_proto(message.fields_by_name["value"]) is stored_value
    assert stored_value.default_value == value_proto.default_value

    other_file = source.pool.FindFileContainingSymbol("other.Message")
    other_message = other_file.message_types_by_name["Message"]
    assert render_descriptor(other_message).startswith(
        "// reflected file route\nmessage other.Message {"
    )

    extension = source.pool.FindExtensionByNumber(message, 100)
    assert render_descriptor(extension).startswith(
        "// reflected extension\nextend .docs.Message {"
    )
    stored_extension = source.file_protos["extension.proto"].extension[0]
    assert field_proto(extension) is stored_extension

    assert source.pool.FindMessageTypeByName("docs.Message") is message
    assert source.pool.FindFileContainingSymbol("other.Message") is other_file
    assert source.pool.FindExtensionByNumber(message, 100) is extension
    assert request_kinds == [
        "list_services",
        "file_containing_symbol",
        "file_containing_symbol",
        "file_containing_extension",
    ]


def test_public_reflection_pool_metadata_is_isolated_by_pool_identity() -> None:
    first_proto = descriptor_pb2.FileDescriptorProto(
        name="same.proto", package="same", syntax="proto3"
    )
    first_proto.message_type.add(name="Message")
    first_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" first reflected pool\n"
    )
    second_proto = descriptor_pb2.FileDescriptorProto()
    second_proto.CopyFrom(first_proto)
    second_proto.source_code_info.location[
        0
    ].leading_comments = " second reflected pool\n"

    def reflected_source(
        file_proto: descriptor_pb2.FileDescriptorProto,
        requests: list[str],
    ) -> ReflectionDescriptorSource:
        class Channel:
            def stream_stream(
                self,
                _path: str,
                *,
                response_deserializer: Any,
                **_kwargs: Any,
            ) -> Any:
                def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                    request = next(request_iterator)
                    kind = request.WhichOneof("message_request")
                    requests.append(kind)
                    response = reflection_pb2.ServerReflectionResponse()
                    _echo_request(response, request)
                    if kind == "list_services":
                        response.list_services_response.SetInParent()
                    else:
                        response.file_descriptor_response.file_descriptor_proto.append(
                            file_proto.SerializeToString()
                        )
                    return _Call(
                        response_deserializer(response.SerializeToString())
                    )

                return rpc

        grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
        return ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    first_requests: list[str] = []
    second_requests: list[str] = []
    first = reflected_source(first_proto, first_requests)
    second = reflected_source(second_proto, second_requests)
    first_message = first.pool.FindMessageTypeByName("same.Message")
    second_message = second.pool.FindMessageTypeByName("same.Message")

    assert render_descriptor(second_message).startswith(
        "// second reflected pool\nmessage same.Message"
    )
    assert render_descriptor(first_message).startswith(
        "// first reflected pool\nmessage same.Message"
    )
    assert render_descriptor(second_message).startswith(
        "// second reflected pool\nmessage same.Message"
    )
    assert first_requests == ["list_services", "file_containing_symbol"]
    assert second_requests == ["list_services", "file_containing_symbol"]


def test_descriptor_source_metadata_override_takes_priority_over_pool() -> None:
    pool_proto = descriptor_pb2.FileDescriptorProto(
        name="priority.proto", package="priority", syntax="proto3"
    )
    pool_proto.message_type.add(name="Message")
    pool_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" pool metadata\n"
    )
    override_proto = descriptor_pb2.FileDescriptorProto()
    override_proto.CopyFrom(pool_proto)
    override_proto.source_code_info.location[
        0
    ].leading_comments = " descriptor metadata\n"
    pool = descriptor_pool.DescriptorPool()
    pool.Add(pool_proto)
    register_pool_file_protos(pool, {pool_proto.name: pool_proto})
    message = pool.FindMessageTypeByName("priority.Message")

    assert render_descriptor(message).startswith(
        "// pool metadata\nmessage priority.Message"
    )
    register_file_proto(message.file, override_proto)

    assert render_descriptor(message).startswith(
        "// descriptor metadata\nmessage priority.Message"
    )
