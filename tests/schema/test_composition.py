from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial.presentation.descriptor import render_descriptor
from protodial.schema.composition import CompositeDescriptorSource
from protodial.schema.factory import build_descriptor_source
from protodial.schema.local import (
    ProtosetDescriptorSource,
)
from protodial.schema.reflection import ReflectionDescriptorSource
from protodial.schema.source_protos import (
    field_proto,
)

from .support import (
    _Call,
    _command,
    _echo_request,
    _file,
    _RpcError,
    _StatusCode,
    _write_set,
)


def test_composite_source_resolves_local_and_reflection_symbols(
    tmp_path: Path,
) -> None:
    local_proto = _file("local.proto", "local")
    remote_proto = _file("remote.proto", "remote")
    local = ProtosetDescriptorSource(
        (_write_set(tmp_path / "local.pb", local_proto),)
    )

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if request.WhichOneof("message_request") == "list_services":
                    response.list_services_response.service.add(
                        name="remote.API"
                    )
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        remote_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    reflection = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)
    source = CompositeDescriptorSource(local, reflection)

    assert source.list_services() == ("local.API", "remote.API")
    assert source.find_symbol("local.API/Call").full_name == "local.API.Call"
    assert source.find_symbol("remote.API/Call").full_name == "remote.API.Call"


def test_public_composite_pool_preserves_lazy_reflection_source_metadata(
    tmp_path: Path,
) -> None:
    local = ProtosetDescriptorSource(
        (_write_set(tmp_path / "local.pb", _file("local.proto", "local")),)
    )
    remote_proto = descriptor_pb2.FileDescriptorProto(
        name="remote.proto", package="remote", syntax="proto3"
    )
    remote_message = remote_proto.message_type.add(name="Message")
    remote_message.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    remote_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" composite reflection\n"
    )
    remote_proto.source_code_info.location.add(
        path=(4, 0, 2, 0), leading_comments=" composite field\n"
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
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        remote_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    reflection = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)
    source = CompositeDescriptorSource(local, reflection)

    message = source.pool.FindMessageTypeByName("remote.Message")

    assert render_descriptor(message).startswith(
        "// composite reflection\nmessage remote.Message {"
    )
    assert "// composite field" in render_descriptor(message)
    stored_field = source.file_protos["remote.proto"].message_type[0].field[0]
    assert field_proto(message.fields_by_name["value"]) is stored_field
    assert source.pool.FindMessageTypeByName("remote.Message") is message
    assert request_kinds == ["list_services", "file_containing_symbol"]


def test_common_builder_defaults_local_sources_to_no_reflection(
    tmp_path: Path,
) -> None:
    schema_path = _write_set(
        tmp_path / "local.pb", _file("local.proto", "local")
    )
    schema = SimpleNamespace(
        proto=(),
        proto_path=(),
        protoset=(schema_path,),
        protoset_out=None,
        use_reflection=False,
    )

    source = build_descriptor_source(_command(schema), SimpleNamespace())

    assert isinstance(source, ProtosetDescriptorSource)
    assert source.list_services() == ("local.API",)
