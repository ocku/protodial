"""Coordinate RPC invocation and schema discovery for the CLI."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator, Iterable, Iterator
from typing import TYPE_CHECKING, BinaryIO, TypeVar, cast, overload

from .commands.models import Command, DiagnosticOptions, InvokeOptions
from .errors import StdoutBrokenPipe
from .message_io.output import write_response
from .request_metadata.types import Metadata
from .rpc_core import call as _call
from .rpc_core import discovery as _discovery
from .rpc_core import invoke as _invoke
from .rpc_core.input import (
    open_input,
    parse_metadata,
    read_exact,
    select_bytes_field,
)
from .rpc_core.models import MessageCounter as _MessageCounter
from .rpc_core.models import RpcFailure, RpcSummary
from .rpc_core.protocols import (
    MethodStreaming,
    ResponseWriter,
    RpcCallable,
    RpcChannel,
    RpcRuntime,
    StreamStreamCallable,
    StreamStreamMethod,
    StreamUnaryCallable,
    StreamUnaryMethod,
    UnaryStreamCallable,
    UnaryStreamMethod,
    UnaryUnaryCallable,
    UnaryUnaryMethod,
)
from .rpc_core.runtime import load_runtime
from .transport_core.protocols import Channel, GrpcTransport

if TYPE_CHECKING:
    from google.protobuf.descriptor import MethodDescriptor
    from google.protobuf.descriptor_pool import DescriptorPool
    from google.protobuf.message import Message

    from .schema.contracts import DescriptorSource, GrpcModule

_RequestT = TypeVar("_RequestT")
_ResponseT = TypeVar("_ResponseT")


class _CliStdout:
    """Mark pipe failures only while bytes are written to CLI stdout."""

    def write(self, value: bytes | bytearray | memoryview[bytes]) -> int:
        try:
            return cast(BinaryIO, sys.stdout.buffer).write(value)
        except BrokenPipeError as error:
            raise StdoutBrokenPipe from error

    def flush(self) -> None:
        try:
            cast(BinaryIO, sys.stdout.buffer).flush()
        except BrokenPipeError as error:
            raise StdoutBrokenPipe from error


_CLI_STDOUT = cast(BinaryIO, _CliStdout())


def create_channel(command: Command, grpc: GrpcTransport) -> Channel:
    """Create a channel through the transport layer."""
    from .transport import create_channel as create

    return create(command, grpc)


def create_rpc(
    channel: RpcChannel,
    method: MethodDescriptor,
    request_class: type[Message],
    response_class: type[Message],
) -> RpcCallable[Message, Message]:
    """Create a grpcio callable for one protobuf method."""
    return _call.create_rpc(channel, method, request_class, response_class)


def count_messages(
    iterator: Iterator[object], counter: _MessageCounter
) -> Iterator[object]:
    """Yield messages while updating a shared counter."""
    return _call.count_messages(iterator, counter)


def normalize_metadata(
    metadata: Iterable[tuple[object, str | bytes]] | None,
) -> Metadata:
    """Normalize grpcio metadata keys to strings."""
    return _call.normalize_metadata(metadata)


@overload
def invoke_rpc(
    rpc: UnaryUnaryCallable[_RequestT, _ResponseT],
    request_iterator: Iterator[_RequestT],
    options: InvokeOptions,
    timeout: float,
    method: UnaryUnaryMethod,
    metadata: Metadata,
    pool: DescriptorPool | None,
    output_field: str | None,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
    compression: object | None = None,
) -> RpcSummary: ...


@overload
def invoke_rpc(
    rpc: UnaryStreamCallable[_RequestT, _ResponseT],
    request_iterator: Iterator[_RequestT],
    options: InvokeOptions,
    timeout: float,
    method: UnaryStreamMethod,
    metadata: Metadata,
    pool: DescriptorPool | None,
    output_field: str | None,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
    compression: object | None = None,
) -> RpcSummary: ...


@overload
def invoke_rpc(
    rpc: StreamUnaryCallable[_RequestT, _ResponseT],
    request_iterator: Iterator[_RequestT],
    options: InvokeOptions,
    timeout: float,
    method: StreamUnaryMethod,
    metadata: Metadata,
    pool: DescriptorPool | None,
    output_field: str | None,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
    compression: object | None = None,
) -> RpcSummary: ...


@overload
def invoke_rpc(
    rpc: StreamStreamCallable[_RequestT, _ResponseT],
    request_iterator: Iterator[_RequestT],
    options: InvokeOptions,
    timeout: float,
    method: StreamStreamMethod,
    metadata: Metadata,
    pool: DescriptorPool | None,
    output_field: str | None,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
    compression: object | None = None,
) -> RpcSummary: ...


def invoke_rpc(
    rpc: RpcCallable[_RequestT, _ResponseT],
    request_iterator: Iterator[_RequestT],
    options: InvokeOptions,
    timeout: float,
    method: MethodStreaming,
    metadata: Metadata,
    pool: DescriptorPool | None,
    output_field: str | None,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
    compression: object | None = None,
) -> RpcSummary:
    """Execute a prepared RPC callable and collect its result."""
    return _call.invoke_descriptor_rpc(
        rpc,
        request_iterator,
        options,
        timeout,
        method,
        metadata,
        pool,
        output_field,
        grpc,
        diagnostics,
        compression,
        response_writer=cast("ResponseWriter[_ResponseT]", write_response),
    )


def invoke(command: Command, grpc: GrpcModule) -> RpcSummary:
    """Invoke the method selected by a validated command."""

    def write_cli_response(*args: object, **kwargs: object) -> None:
        kwargs["output"] = _CLI_STDOUT
        write_response(*args, **kwargs)  # type: ignore[arg-type]

    return _invoke.invoke(
        command,
        grpc,
        response_writer=cast("ResponseWriter[Message]", write_cli_response),
    )


@contextlib.contextmanager
def open_descriptor_source(
    command: Command, grpc: GrpcModule
) -> Generator[DescriptorSource]:
    """Open the descriptor sources selected by a command."""
    with _discovery.open_descriptor_source(command, grpc) as source:
        yield source


def list_symbols(command: Command, grpc: GrpcModule) -> None:
    """Print services or methods selected by a list command."""
    _discovery.list_symbols(command, grpc, source_opener=open_descriptor_source)


def describe(command: Command, grpc: GrpcModule) -> None:
    """Print the schema symbol selected by a describe command."""
    _discovery.describe(command, grpc, source_opener=open_descriptor_source)


__all__ = [
    "RpcFailure",
    "RpcSummary",
    "count_messages",
    "create_channel",
    "create_rpc",
    "describe",
    "invoke",
    "invoke_rpc",
    "list_symbols",
    "load_runtime",
    "normalize_metadata",
    "open_descriptor_source",
    "open_input",
    "parse_metadata",
    "read_exact",
    "select_bytes_field",
    "write_response",
]
