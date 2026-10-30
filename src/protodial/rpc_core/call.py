"""Low-level RPC construction, execution, and result collection."""

from __future__ import annotations

import time
from collections.abc import Iterator
from functools import partial
from typing import TYPE_CHECKING, TypeVar, cast, overload

from ..commands.models import DiagnosticOptions, InvokeOptions
from ..errors import add_secondary_error
from ..request_metadata.types import Metadata
from .call_results import (
    collect_call_summary,
    write_initial_diagnostics,
    write_terminal_diagnostics,
)
from .call_results import (
    diagnostic_sink as _diagnostic_sink,
)
from .call_results import (
    normalize_metadata as normalize_metadata,
)
from .call_results import (
    summarize_call as summarize_call,
)
from .execution import (
    Execution as _Execution,
)
from .execution import (
    complete_execution as _complete_execution,
)
from .execution import (
    execution_action as _execution_action,
)
from .execution import (
    prepare_request as _prepare_request,
)
from .execution import (
    run_execution as _run_execution,
)
from .models import MessageCounter, RpcSummary
from .protocols import (
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
from .request_stream import count_messages as count_messages

if TYPE_CHECKING:
    from google.protobuf.descriptor import MethodDescriptor
    from google.protobuf.descriptor_pool import DescriptorPool
    from google.protobuf.message import Message


_RequestT = TypeVar("_RequestT")
_ResponseT = TypeVar("_ResponseT")


def create_rpc(
    channel: RpcChannel,
    method: MethodDescriptor,
    request_class: type[Message],
    response_class: type[Message],
) -> RpcCallable[Message, Message]:
    """Create the grpcio callable for a protobuf method descriptor."""
    cardinality = (
        "stream" if method.client_streaming else "unary",
        "stream" if method.server_streaming else "unary",
    )
    if cardinality == ("unary", "unary"):
        factory = channel.unary_unary
    elif cardinality == ("unary", "stream"):
        factory = channel.unary_stream
    elif cardinality == ("stream", "unary"):
        factory = channel.stream_unary
    else:
        factory = channel.stream_stream
    service = method.containing_service
    return factory(
        f"/{service.full_name}/{method.name}",
        request_serializer=request_class.SerializeToString,
        response_deserializer=response_class.FromString,
    )


def _call_options(
    options: InvokeOptions,
    timeout: float,
    metadata: Metadata,
    compression: object | None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "metadata": metadata,
        "timeout": timeout,
        "wait_for_ready": options.wait_for_ready,
    }
    if compression is not None:
        values["compression"] = compression
    return values


def _write_interrupted_diagnostics(
    execution: _Execution[object],
    sent_messages: int,
    started_at: float,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
) -> None:
    call = execution.call
    if call is None or not execution.terminal:
        return
    summary = collect_call_summary(
        call,
        execution.initial_metadata,
        sent_messages,
        execution.received_messages,
        started_at,
        grpc,
    )
    if not execution.initial_diagnostics_written:
        write_initial_diagnostics(
            summary.initial_metadata,
            diagnostics,
            sink=execution.diagnostic_sink,
        )
    write_terminal_diagnostics(
        summary,
        diagnostics,
        include_non_ok_status=True,
        sink=execution.diagnostic_sink,
    )


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
    *,
    response_writer: ResponseWriter[_ResponseT],
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
    *,
    response_writer: ResponseWriter[_ResponseT],
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
    *,
    response_writer: ResponseWriter[_ResponseT],
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
    *,
    response_writer: ResponseWriter[_ResponseT],
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
    *,
    response_writer: ResponseWriter[_ResponseT],
) -> RpcSummary:
    """Execute one RPC and return its terminal summary."""
    started_at = time.monotonic()
    sent = MessageCounter()
    request, request_stream = _prepare_request(request_iterator, method, sent)
    diagnostic_sink = _diagnostic_sink(diagnostics)
    execution: _Execution[_ResponseT] = _Execution(
        on_initial_metadata=partial(
            write_initial_diagnostics,
            diagnostics=diagnostics,
            sink=diagnostic_sink,
        ),
        diagnostic_sink=diagnostic_sink,
    )

    def write_response(message: _ResponseT) -> None:
        response_writer(message, options, method, pool, output_field)

    action = _execution_action(
        rpc,
        request,
        request_stream,
        method,
        _call_options(options, timeout, metadata, compression),
        execution,
        write_response,
    )

    try:
        _run_execution(
            action,
            request_stream,
            execution,
            grpc.RpcError,
        )
    except KeyboardInterrupt as interruption:
        try:
            _write_interrupted_diagnostics(
                cast(_Execution[object], execution),
                sent.value,
                started_at,
                grpc,
                diagnostics,
            )
        except BaseException as diagnostic_error:
            add_secondary_error(
                interruption,
                "Interrupted RPC diagnostics also failed",
                diagnostic_error,
            )
        raise
    return _complete_execution(
        execution,
        request_stream,
        sent.value,
        started_at,
        pool,
        grpc,
        diagnostics,
    )


def invoke_descriptor_rpc(
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
    *,
    response_writer: ResponseWriter[_ResponseT],
) -> RpcSummary:
    """Invoke a callable created from the supplied runtime descriptor.

    This is the dynamic boundary used immediately after :func:`create_rpc`.
    Public prepared-call APIs use the cardinality-correlated overloads on
    :func:`invoke_rpc` instead.
    """
    if method.client_streaming:
        if method.server_streaming:
            return invoke_rpc(
                cast(StreamStreamCallable[_RequestT, _ResponseT], rpc),
                request_iterator,
                options,
                timeout,
                cast(StreamStreamMethod, method),
                metadata,
                pool,
                output_field,
                grpc,
                diagnostics,
                compression,
                response_writer=response_writer,
            )
        return invoke_rpc(
            cast(StreamUnaryCallable[_RequestT, _ResponseT], rpc),
            request_iterator,
            options,
            timeout,
            cast(StreamUnaryMethod, method),
            metadata,
            pool,
            output_field,
            grpc,
            diagnostics,
            compression,
            response_writer=response_writer,
        )
    if method.server_streaming:
        return invoke_rpc(
            cast(UnaryStreamCallable[_RequestT, _ResponseT], rpc),
            request_iterator,
            options,
            timeout,
            cast(UnaryStreamMethod, method),
            metadata,
            pool,
            output_field,
            grpc,
            diagnostics,
            compression,
            response_writer=response_writer,
        )
    return invoke_rpc(
        cast(UnaryUnaryCallable[_RequestT, _ResponseT], rpc),
        request_iterator,
        options,
        timeout,
        cast(UnaryUnaryMethod, method),
        metadata,
        pool,
        output_field,
        grpc,
        diagnostics,
        compression,
        response_writer=response_writer,
    )
