from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial.errors import CliError
from protodial.schema.reflection import ReflectionDescriptorSource

from .support import _Call, _echo_request, _file, _RpcError, _StatusCode


def test_suffix_index_refreshes_after_public_pool_loads_a_file() -> None:
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
                elif value == "missing.Unknown":
                    response.error_response.error_code = 5
                    response.error_response.error_message = "not found"
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

    with pytest.raises(CliError, match="symbol 'Hidden' not found"):
        source.find_symbol("Hidden")
    stale_symbols = source._symbols

    assert (
        source.pool.FindMessageTypeByName("orphan.Hidden").full_name
        == "orphan.Hidden"
    )
    assert source._symbols is stale_symbols
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"

    refreshed_symbols = source._symbols
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    assert source._symbols is refreshed_symbols
    with pytest.raises(CliError, match="symbol 'missing.Unknown' not found"):
        source.find_symbol("missing.Unknown")
    with pytest.raises(CliError, match="symbol 'missing.Unknown' not found"):
        source.find_symbol("missing.Unknown")
    assert source._symbols is refreshed_symbols
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "api.API"),
        ("file_containing_symbol", "orphan.Hidden"),
        ("file_containing_symbol", "missing.Unknown"),
    ]


def test_suffix_index_refreshes_after_public_database_loads_a_file() -> None:
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
                        orphan_proto if value == "orphan.proto" else api_proto
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

    with pytest.raises(CliError, match="symbol 'Hidden' not found"):
        source.find_symbol("Hidden")
    source.database.FindFileByName("orphan.proto")

    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    cached_symbols = source._symbols
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    assert source._symbols is cached_symbols
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "api.API"),
        ("file_by_filename", "orphan.proto"),
    ]


def test_suffix_index_refreshes_after_extension_lookup_loads_files() -> None:
    api_proto = _file("api.proto", "api")
    base_proto = descriptor_pb2.FileDescriptorProto(
        name="base.proto", package="base", syntax="proto2"
    )
    target = base_proto.message_type.add(name="Target")
    target.extension_range.add(start=100, end=101)
    extension_proto = descriptor_pb2.FileDescriptorProto(
        name="extension.proto",
        package="extra",
        syntax="proto2",
        dependency=("base.proto",),
    )
    extension_proto.extension.add(
        name="note",
        number=100,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".base.Target",
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
                    response.list_services_response.service.add(name="api.API")
                elif kind == "file_containing_extension":
                    payload = response.file_descriptor_response
                    payload.file_descriptor_proto.extend(
                        (
                            extension_proto.SerializeToString(),
                            base_proto.SerializeToString(),
                        )
                    )
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        api_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    with pytest.raises(CliError, match="symbol 'note' not found"):
        source.find_symbol("note")
    source.database.FindFileContainingExtension("base.Target", 100)

    assert source.find_symbol("note").full_name == "extra.note"
    cached_symbols = source._symbols
    assert source.find_symbol("note").full_name == "extra.note"
    assert source._symbols is cached_symbols
    assert request_kinds == [
        "list_services",
        "file_containing_symbol",
        "file_containing_extension",
    ]
