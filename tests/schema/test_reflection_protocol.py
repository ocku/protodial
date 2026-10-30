from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from grpc_reflection.v1alpha import reflection_pb2

from protodial.errors import CliError
from protodial.schema.reflection import (
    ReflectionDescriptorSource,
    reflection_database,
)

from .support import _Call, _echo_request, _grpc, _StatusCode


def test_protocol_unimplemented_response_falls_back_without_pinning_v1() -> (
    None
):
    paths: list[str] = []

    class Channel:
        def stream_stream(
            self, path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                paths.append(path)
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if ".v1." in path:
                    response.error_response.error_code = (
                        _StatusCode.UNIMPLEMENTED.value[0]
                    )
                    response.error_response.error_message = (
                        "stable reflection is disabled"
                    )
                else:
                    response.list_services_response.service.add(name="edge.API")
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    source = ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())

    assert source.list_services() == ("edge.API",)
    assert source.database.version == "v1alpha"
    assert paths == [
        "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
        "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
    ]


def test_reflection_response_double_need_not_implement_which_oneof() -> None:
    class Response:
        error_response = SimpleNamespace(error_code=0, error_message="")
        file_descriptor_response = SimpleNamespace(file_descriptor_proto=())
        all_extension_numbers_response = SimpleNamespace(extension_number=())
        list_services_response = SimpleNamespace(
            service=(SimpleNamespace(name="edge.API"),)
        )

        valid_host = ""

        def __init__(self, original_request: Any) -> None:
            self.original_request = original_request

        def HasField(self, field_name: str) -> bool:
            return field_name in {"original_request", "list_services_response"}

    class Channel:
        def stream_stream(self, _path: str, **_kwargs: Any) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                return _Call(Response(next(requests)))

            return rpc

    source = ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())

    assert source.list_services() == ("edge.API",)


def test_reflection_rejects_response_variant_for_another_request() -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.file_descriptor_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    with pytest.raises(CliError, match="expected 'list_services_response'"):
        ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())


def test_reflection_rejects_corrupt_file_descriptor_payload() -> None:
    cancelled: list[bool] = []

    class RecordingCall(_Call):
        def cancel(self) -> None:
            cancelled.append(True)
            raise RuntimeError("cancel cleanup")

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> RecordingCall:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.file_descriptor_response.file_descriptor_proto.append(
                    b"\xff"
                )
                return RecordingCall(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError) as raised:
        database.query(file_by_filename="corrupt.proto")

    assert str(raised.value) == (
        "reflection protocol error: invalid file descriptor payload"
    )
    assert raised.value.__cause__ is None
    assert cancelled == [True]
    assert getattr(raised.value, "_protodial_notes") == (
        "reflection cancellation also failed: cancel cleanup",
    )
