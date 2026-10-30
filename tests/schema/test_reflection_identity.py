from __future__ import annotations

from typing import Any

import pytest
from grpc_reflection.v1alpha import reflection_pb2

from protodial.errors import CliError
from protodial.schema.reflection import (
    ReflectionDescriptorSource,
    reflection_database,
)

from .support import _Call, _echo_request, _grpc


@pytest.mark.parametrize(
    ("echo_field", "echo_value"),
    [
        ("file_by_filename", "other.proto"),
        ("file_containing_symbol", "other.Symbol"),
    ],
)
def test_reflection_rejects_same_variant_response_for_another_request(
    echo_field: str, echo_value: str
) -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(_requests: Any, **_call_kwargs: Any) -> _Call:
                response = reflection_pb2.ServerReflectionResponse()
                setattr(response.original_request, echo_field, echo_value)
                response.file_descriptor_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError, match="original_request does not match"):
        database.query(file_containing_symbol="wanted.Symbol")


def test_reflection_rejects_missing_request_echo() -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(_requests: Any, **_call_kwargs: Any) -> _Call:
                response = reflection_pb2.ServerReflectionResponse()
                response.list_services_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError, match="no original_request"):
        database.query(list_services="")


def test_reflection_rejects_wrong_valid_host() -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse(
                    valid_host="wrong.example"
                )
                _echo_request(response, request)
                response.list_services_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(
        Channel(), [], 1.0, _grpc(), host="right.example"
    )

    with pytest.raises(CliError, match="valid_host 'wrong.example'"):
        database.query(list_services="")


def test_reflection_preserves_primary_when_cancellation_fails() -> None:
    primary = CliError("reflection primary")

    class FailingCall:
        def __iter__(self) -> FailingCall:
            return self

        def __next__(self) -> Any:
            raise primary

        def cancel(self) -> None:
            raise OSError("cancel cleanup")

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> FailingCall:
                next(requests)
                return FailingCall()

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError, match="reflection primary") as raised:
        database.query(list_services="")

    assert raised.value is primary
    assert getattr(primary, "_protodial_notes") == (
        "reflection cancellation also failed: cancel cleanup",
    )


@pytest.mark.parametrize(
    "name",
    [
        "safe.API\nforged",
        "safe.API\rforged",
        "safe.API\x1b[2Jforged",
        "safe.API\u202eforged",
    ],
)
def test_reflection_rejects_invalid_service_names(name: str) -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.list_services_response.service.add(name=name)
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    with pytest.raises(
        CliError,
        match=r"^reflection protocol error: invalid service name$",
    ) as raised:
        ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())

    assert "safe.API" not in str(raised.value)
