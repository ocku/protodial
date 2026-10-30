from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial.presentation.descriptor import render_descriptor
from protodial.schema.reflection import ReflectionDescriptorSource

from .support import _Call, _echo_request, _file, _RpcError, _StatusCode


def test_exact_reflection_lookup_registers_metadata_before_indexing() -> None:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="docs.proto", package="docs", syntax="proto3"
    )
    message = file_proto.message_type.add(name="Message")
    message.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    service = file_proto.service.add(name="API")
    service.method.add(
        name="Call",
        input_type=".docs.Message",
        output_type=".docs.Message",
    )
    message_location = file_proto.source_code_info.location.add()
    message_location.path.extend((4, 0))
    message_location.leading_comments = " Exact message documentation.\n"
    field_location = file_proto.source_code_info.location.add()
    field_location.path.extend((4, 0, 2, 0))
    field_location.leading_comments = " Exact field documentation.\n"
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="docs.API")
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        file_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    field = source.find_symbol("docs.Message.value")
    assert source._symbols is None
    assert render_descriptor(field).startswith(
        "// Exact field documentation.\nstring value = 1;"
    )

    message_descriptor = source.find_symbol("docs.Message")
    assert render_descriptor(message_descriptor).startswith(
        "// Exact message documentation.\nmessage docs.Message {"
    )
    assert "// Exact field documentation." in render_descriptor(
        message_descriptor
    )

    assert source.find_symbol("Message").full_name == "docs.Message"
    assert render_descriptor(field).startswith("// Exact field documentation.")
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "docs.Message.value"),
    ]


def test_exact_reflection_lookup_before_index_includes_unlinked_file() -> None:
    api_proto = _file("api.proto", "api")
    orphan_proto = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan_proto.message_type.add(name="Hidden")
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                else:
                    selected = (
                        orphan_proto if value == "orphan.Hidden" else api_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "orphan.Hidden"),
        ("file_containing_symbol", "api.API"),
    ]
