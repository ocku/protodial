from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial.schema.reflection import ReflectionDescriptorSource

from .support import _Call, _echo_request, _file, _RpcError, _StatusCode


def test_reflection_uses_version_specific_wire_classes() -> None:
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                requests.append((path, request.DESCRIPTOR.full_name))
                if ".v1." in path:
                    raise _RpcError()
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.list_services_response.service.add(name="remote.API")
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    assert source.list_services() == ("remote.API",)
    assert requests == [
        (
            "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
            "grpc.reflection.v1.ServerReflectionRequest",
        ),
        (
            "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
            "grpc.reflection.v1alpha.ServerReflectionRequest",
        ),
    ]


def test_reflection_resolves_symbol_outside_advertised_service_graph() -> None:
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
    assert ("file_containing_symbol", "orphan.Hidden") in requests


def test_exact_reflection_lookup_refreshes_a_primed_suffix_index() -> None:
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

    # Build the suffix index before the unlinked file is known.
    assert source.find_symbol("Message").full_name == "api.Message"
    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"

    # An already-loaded exact lookup neither queries reflection nor invalidates
    # the refreshed index.
    cached_symbols = source._symbols
    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert source._symbols is cached_symbols
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "api.API"),
        ("file_containing_symbol", "orphan.Hidden"),
    ]
