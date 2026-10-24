"""Low-level RPC construction, execution, and result collection."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import partial
from threading import Event
from typing import TYPE_CHECKING, Generic, TypeVar, cast, overload

from ..commands.models import DiagnosticOptions, InvokeOptions
from ..errors import CliError
from ..request_metadata.types import Metadata
from .models import MessageCounter, RpcFailure, RpcSummary
from .protocols import (
    MethodStreaming,
    ResponseWriter,
    RpcCall,
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

if TYPE_CHECKING:
    from google.protobuf.descriptor import MethodDescriptor
    from google.protobuf.descriptor_pool import DescriptorPool
    from google.protobuf.message import Message

    from ..presentation.diagnostics.models import DiagnosticSink


_MessageT = TypeVar("_MessageT")
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


def count_messages(
    iterator: Iterator[_MessageT], counter: MessageCounter
) -> Iterator[_MessageT]:
    """Yield messages while updating a shared counter."""
    for message in iterator:
        counter.value += 1
        yield message


def normalize_metadata(
    metadata: Iterable[tuple[object, str | bytes]] | None,
) -> Metadata:
    """Normalize grpcio metadata keys to strings."""
    if metadata is None:
        return []
    return [(str(key), value) for key, value in metadata]


@dataclass
class RequestStream(Generic[_RequestT], Iterator[_RequestT]):
    """Track a client stream without joining the request-consumer thread."""

    iterator: Iterator[_RequestT]
    counter: MessageCounter
    completed: Event = field(default_factory=Event)
    stopped: Event = field(default_factory=Event)
    error: BaseException | None = None

    def __iter__(self) -> RequestStream[_RequestT]:
        return self

    def __next__(self) -> _RequestT:
        if self.stopped.is_set():
            self.completed.set()
            raise StopIteration
        try:
            message = next(self.iterator)
        except StopIteration:
            self.completed.set()
            raise
        except BaseException as error:
            # grpcio consumes request iterators on a worker thread and otherwise
            # replaces producer failures with a generic RPC error. Preserve the
            # original exception, including programming errors and interrupts.
            self.error = error
            self.completed.set()
            raise
        if self.stopped.is_set():
            self.completed.set()
            raise StopIteration
        self.counter.value += 1
        return message

    def stop(self) -> None:
        """Prevent another request after the RPC reaches a terminal state."""
        self.stopped.set()


def summarize_call(
    call: RpcCall[object],
    initial_metadata: Metadata,
    sent_messages: int,
    received_messages: int,
    started_at: float,
    grpc: RpcRuntime,
) -> RpcSummary:
    """Collect terminal metadata and status from a call."""
    trailing = call.trailing_metadata()
    unknown = grpc.StatusCode.UNKNOWN
    code = call.code()
    details = call.details()
    return RpcSummary(
        initial_metadata=initial_metadata,
        trailing_metadata=normalize_metadata(trailing),
        code=unknown if code is None else code,
        details=str(details),
        sent_messages=sent_messages,
        received_messages=received_messages,
        elapsed_seconds=time.monotonic() - started_at,
    )


def _diagnostic_sink(diagnostics: DiagnosticOptions) -> DiagnosticSink:
    """Return the active sink or a standalone command-line sink."""
    from ..presentation.diagnostics.context import current_sink
    from ..presentation.diagnostics.rendering import StderrDiagnosticSink

    return current_sink() or StderrDiagnosticSink(
        diagnostics.verbosity,
        diagnostic_format=diagnostics.diagnostic_format,
        color=diagnostics.color,
    )


def write_initial_diagnostics(
    metadata: Metadata,
    diagnostics: DiagnosticOptions,
    *,
    sink: DiagnosticSink | None = None,
) -> None:
    """Emit response headers when they arrive from the server."""
    from ..presentation.diagnostics.models import DiagnosticEvent

    if diagnostics.show_headers:
        output = sink if sink is not None else _diagnostic_sink(diagnostics)
        output.emit(
            DiagnosticEvent(
                "rpc.headers",
                level=0,
                metadata=tuple(metadata),
            )
        )


def write_terminal_diagnostics(
    summary: RpcSummary,
    diagnostics: DiagnosticOptions,
    *,
    include_non_ok_status: bool = False,
    sink: DiagnosticSink | None = None,
) -> None:
    """Emit response trailers and status after the body completes."""
    from ..presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
        DiagnosticValue,
    )

    output = sink if sink is not None else _diagnostic_sink(diagnostics)
    if diagnostics.show_trailers:
        output.emit(
            DiagnosticEvent(
                "rpc.trailers",
                level=0,
                metadata=tuple(summary.trailing_metadata),
            )
        )
    code_name = getattr(summary.code, "name", str(summary.code))
    if diagnostics.show_status and (code_name == "OK" or include_non_ok_status):
        from ..presentation.rpc import status_parts

        _, code_number = status_parts(summary.code)
        fields: dict[str, DiagnosticValue] = {
            "status": code_name,
            "grpc_status": {
                "name": code_name,
                "number": code_number,
            },
            "elapsed_seconds": summary.elapsed_seconds,
            "sent": summary.sent_messages,
            "received": summary.received_messages,
            "sent_messages": summary.sent_messages,
            "received_messages": summary.received_messages,
        }
        output.emit(
            DiagnosticEvent(
                "rpc.completed",
                level=0,
                severity=(
                    DiagnosticSeverity.STATUS
                    if code_name == "OK"
                    else DiagnosticSeverity.WARNING
                ),
                code=None if code_name == "OK" else "W4101",
                fields=fields,
                causes=(
                    (summary.details,)
                    if code_name != "OK" and summary.details
                    else ()
                ),
            )
        )


def collect_call_summary(
    call: RpcCall[object],
    initial_metadata: Metadata,
    sent_messages: int,
    received_messages: int,
    started_at: float,
    grpc: RpcRuntime,
) -> RpcSummary:
    """Collect one terminal call result without presenting it."""
    if not initial_metadata:
        metadata = call.initial_metadata()
        initial_metadata = normalize_metadata(metadata)
    return summarize_call(
        call,
        initial_metadata,
        sent_messages,
        received_messages,
        started_at,
        grpc,
    )


@dataclass
class _Execution(Generic[_ResponseT]):
    """Mutable state collected while one RPC is active."""

    call: RpcCall[_ResponseT] | None = None
    initial_metadata: Metadata = field(default_factory=lambda: [])
    received_messages: int = 0
    failure: Exception | None = None
    terminal: bool = False
    initial_diagnostics_written: bool = False
    on_initial_metadata: Callable[[Metadata], None] | None = None
    diagnostic_sink: DiagnosticSink | None = None


def _prepare_request(
    request_iterator: Iterator[_RequestT],
    method: MethodStreaming,
    sent: MessageCounter,
) -> tuple[_RequestT | Iterator[_RequestT], RequestStream[_RequestT] | None]:
    """Prepare one unary request or a tracked client request stream."""
    if method.client_streaming:
        stream = RequestStream(request_iterator, sent)
        return stream, stream
    requests = count_messages(request_iterator, sent)
    try:
        return next(requests), None
    except StopIteration:
        raise CliError("request input is empty") from None


def _collect_server_stream(
    call: RpcCall[_ResponseT],
    request_stream: RequestStream[_RequestT] | None,
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> None:
    """Collect and render an already-started streaming response."""
    execution.call = call
    execution.initial_metadata = normalize_metadata(call.initial_metadata())
    if execution.on_initial_metadata is not None:
        execution.on_initial_metadata(execution.initial_metadata)
    execution.initial_diagnostics_written = True
    for message in call:
        write_response(message)
        execution.received_messages += 1
        if request_stream is not None and request_stream.error is not None:
            call.cancel()
            break
    execution.terminal = True


def _collect_unary_response(
    call: RpcCall[_ResponseT],
    request_stream: RequestStream[_RequestT] | None,
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> None:
    """Collect and render an already-started unary response."""
    execution.call = call
    execution.initial_metadata = normalize_metadata(call.initial_metadata())
    if execution.on_initial_metadata is not None:
        execution.on_initial_metadata(execution.initial_metadata)
    execution.initial_diagnostics_written = True
    response = call.result()
    execution.terminal = True
    if request_stream is None or request_stream.error is None:
        write_response(response)
        execution.received_messages = 1


def _execute_unary_unary(
    rpc: UnaryUnaryCallable[_RequestT, _ResponseT],
    request: _RequestT,
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> None:
    """Execute a unary-request, unary-response RPC."""
    _collect_unary_response(
        rpc.future(request, **call_options),
        None,
        execution,
        write_response,
    )


def _execute_unary_stream(
    rpc: UnaryStreamCallable[_RequestT, _ResponseT],
    request: _RequestT,
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> None:
    """Execute a unary-request, streaming-response RPC."""
    _collect_server_stream(
        rpc(request, **call_options),
        None,
        execution,
        write_response,
    )


def _execute_stream_unary(
    rpc: StreamUnaryCallable[_RequestT, _ResponseT],
    request: RequestStream[_RequestT],
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> None:
    """Execute a streaming-request, unary-response RPC."""
    _collect_unary_response(
        rpc.future(request, **call_options),
        request,
        execution,
        write_response,
    )


def _execute_stream_stream(
    rpc: StreamStreamCallable[_RequestT, _ResponseT],
    request: RequestStream[_RequestT],
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> None:
    """Execute a bidirectional streaming RPC."""
    _collect_server_stream(
        rpc(request, **call_options),
        request,
        execution,
        write_response,
    )


def _streaming_request_action(
    rpc: RpcCallable[_RequestT, _ResponseT],
    request: RequestStream[_RequestT],
    method: MethodStreaming,
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> Callable[[], None]:
    if method.server_streaming:
        return partial(
            _execute_stream_stream,
            cast(StreamStreamCallable[_RequestT, _ResponseT], rpc),
            request,
            call_options,
            execution,
            write_response,
        )
    return partial(
        _execute_stream_unary,
        cast(StreamUnaryCallable[_RequestT, _ResponseT], rpc),
        request,
        call_options,
        execution,
        write_response,
    )


def _unary_request_action(
    rpc: RpcCallable[_RequestT, _ResponseT],
    request: _RequestT,
    method: MethodStreaming,
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> Callable[[], None]:
    if method.server_streaming:
        return partial(
            _execute_unary_stream,
            cast(UnaryStreamCallable[_RequestT, _ResponseT], rpc),
            request,
            call_options,
            execution,
            write_response,
        )
    return partial(
        _execute_unary_unary,
        cast(UnaryUnaryCallable[_RequestT, _ResponseT], rpc),
        request,
        call_options,
        execution,
        write_response,
    )


def _execution_action(
    rpc: RpcCallable[_RequestT, _ResponseT],
    request: _RequestT | Iterator[_RequestT],
    request_stream: RequestStream[_RequestT] | None,
    method: MethodStreaming,
    call_options: dict[str, object],
    execution: _Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> Callable[[], None]:
    if method.client_streaming:
        if request_stream is None:
            raise RuntimeError("streaming RPC did not prepare a request stream")
        return _streaming_request_action(
            rpc,
            request_stream,
            method,
            call_options,
            execution,
            write_response,
        )
    if request_stream is not None or isinstance(request, Iterator):
        raise RuntimeError("unary RPC prepared a request stream")
    return _unary_request_action(
        rpc,
        request,
        method,
        call_options,
        execution,
        write_response,
    )


def _run_execution(
    action: Callable[[], None],
    request_stream: RequestStream[_RequestT] | None,
    execution: _Execution[_ResponseT],
    rpc_error_type: type[Exception],
) -> None:
    """Run the RPC lifecycle and collect state without presenting results."""
    try:
        action()
    except rpc_error_type as error:
        # grpcio's RPC exceptions also implement the Call interface. A failure
        # raised before assigning the returned call therefore remains
        # inspectable.
        execution.call = (
            cast(RpcCall[_ResponseT], error)
            if execution.call is None
            else execution.call
        )
        execution.failure = error
        execution.terminal = True
    finally:
        if request_stream is not None:
            request_stream.stop()
        if execution.call is not None and not execution.terminal:
            execution.call.cancel()


def _complete_execution(
    execution: _Execution[_ResponseT],
    request_stream: RequestStream[_RequestT] | None,
    sent_messages: int,
    started_at: float,
    pool: DescriptorPool | None,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
) -> RpcSummary:
    """Present terminal diagnostics and translate collected failures."""
    call = execution.call
    if call is None:
        raise RuntimeError("gRPC invocation did not provide a call object")
    input_error = request_stream.error if request_stream is not None else None
    if input_error is not None:
        call.cancel()
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
        summary, diagnostics, sink=execution.diagnostic_sink
    )
    if input_error is not None:
        raise input_error from None
    if execution.failure is None:
        return summary

    rich_status = None
    if diagnostics.format_error:
        from ..rich_status.decoding import decode_rich_status

        rich_status = decode_rich_status(
            summary.trailing_metadata,
            pool,
            transport_code=summary.code,
            transport_message=summary.details,
        )
    raise RpcFailure(
        execution.failure,
        summary,
        False,
        rich_status=rich_status,
        descriptor_pool=pool,
    ) from None


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


def _cancel_interrupted_execution(
    execution: _Execution[object],
    sent_messages: int,
    started_at: float,
    grpc: RpcRuntime,
    diagnostics: DiagnosticOptions,
) -> None:
    call = execution.call
    if call is None:
        return
    call.cancel()
    execution.terminal = True
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
    except KeyboardInterrupt:
        _cancel_interrupted_execution(
            cast(_Execution[object], execution),
            sent.value,
            started_at,
            grpc,
            diagnostics,
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
