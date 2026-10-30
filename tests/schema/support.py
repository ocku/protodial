from __future__ import annotations

from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2


class _StatusCode(Enum):
    NOT_FOUND = (5, "not found")
    UNIMPLEMENTED = (12, "unimplemented")


class _RpcError(Exception):
    def code(self) -> _StatusCode:
        return _StatusCode.UNIMPLEMENTED


class _Call:
    def __init__(self, response: Any) -> None:
        self._response = response
        self._used = False

    def __iter__(self) -> _Call:
        return self

    def __next__(self) -> Any:
        if self._used:
            raise StopIteration
        self._used = True
        return self._response

    def cancel(self) -> None:
        pass


def _grpc() -> Any:
    return SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)


def _echo_request(response: Any, request: Any) -> None:
    response.original_request.ParseFromString(request.SerializeToString())


class _DescriptorBatchChannel:
    def __init__(self, *batches: tuple[bytes, ...]) -> None:
        self.batches = list(batches)

    def stream_stream(
        self, _path: str, *, response_deserializer: Any, **_kwargs: Any
    ) -> Any:
        def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
            request = next(requests)
            response = reflection_pb2.ServerReflectionResponse()
            _echo_request(response, request)
            response.file_descriptor_response.file_descriptor_proto.extend(
                self.batches.pop(0)
            )
            return _Call(response_deserializer(response.SerializeToString()))

        return rpc


def _file(
    name: str, package: str, service: str = "API"
) -> descriptor_pb2.FileDescriptorProto:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name=name, package=package, syntax="proto3"
    )
    message = file_proto.message_type.add(name="Message")
    message.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    rpc_service = file_proto.service.add(name=service)
    rpc_service.method.add(
        name="Call",
        input_type=f".{package}.Message",
        output_type=f".{package}.Message",
    )
    return file_proto


def _write_set(path: Path, *files: descriptor_pb2.FileDescriptorProto) -> str:
    path.write_bytes(
        descriptor_pb2.FileDescriptorSet(file=files).SerializeToString()
    )
    return str(path)


def _command(schema: Any) -> Any:
    return SimpleNamespace(schema=schema, timeout=1.0)
