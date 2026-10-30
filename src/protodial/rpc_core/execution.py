"""Execute cardinality-specific RPC actions and collect lifecycle state."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from ..commands.models import DiagnosticOptions
from ..errors import CliError, add_secondary_error
from ..request_metadata.types import Metadata
from .call_results import (
    collect_call_summary,
    normalize_metadata,
    write_initial_diagnostics,
    write_terminal_diagnostics,
)
from .models import MessageCounter, RpcFailure, RpcSummary
from .protocols import (
    MethodStreaming,
    RpcCall,
    RpcCallable,
    RpcRuntime,
    StreamStreamCallable,
    StreamUnaryCallable,
    UnaryStreamCallable,
    UnaryUnaryCallable,
)
from .request_stream import RequestStream, count_messages

if TYPE_CHECKING:
    from google.protobuf.descriptor_pool import DescriptorPool

    from ..presentation.diagnostics.models import DiagnosticSink

_RequestT = TypeVar("_RequestT")
_ResponseT = TypeVar("_ResponseT")


@dataclass
class Execution(Generic[_ResponseT]):
    """Mutable state collected while one RPC is active."""

    call: RpcCall[_ResponseT] | None = None
    initial_metadata: Metadata = field(default_factory=lambda: [])
    received_messages: int = 0
    failure: Exception | None = None
    terminal: bool = False
    initial_diagnostics_written: bool = False
    on_initial_metadata: Callable[[Metadata], None] | None = None
    diagnostic_sink: DiagnosticSink | None = None


def prepare_request(
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
    execution: Execution[_ResponseT],
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
            # Completion cancels once and preserves the producer failure if
            # either cancellation or terminal diagnostics also fail.
            break
    execution.terminal = True


def _collect_unary_response(
    call: RpcCall[_ResponseT],
    request_stream: RequestStream[_RequestT] | None,
    execution: Execution[_ResponseT],
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
    execution: Execution[_ResponseT],
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
    execution: Execution[_ResponseT],
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
    execution: Execution[_ResponseT],
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
    execution: Execution[_ResponseT],
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
    execution: Execution[_ResponseT],
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
    execution: Execution[_ResponseT],
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


def execution_action(
    rpc: RpcCallable[_RequestT, _ResponseT],
    request: _RequestT | Iterator[_RequestT],
    request_stream: RequestStream[_RequestT] | None,
    method: MethodStreaming,
    call_options: dict[str, object],
    execution: Execution[_ResponseT],
    write_response: Callable[[_ResponseT], None],
) -> Callable[[], None]:
    """Select an action matching the RPC request and response cardinality."""
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


def run_execution(
    action: Callable[[], None],
    request_stream: RequestStream[_RequestT] | None,
    execution: Execution[_ResponseT],
    rpc_error_type: type[Exception],
) -> None:
    """Run the RPC lifecycle and collect state without presenting results."""
    primary: BaseException | None = None
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
    except BaseException as error:
        primary = error
        raise
    finally:
        if request_stream is not None:
            request_stream.stop()
        if execution.call is not None and not execution.terminal:
            try:
                execution.call.cancel()
                execution.terminal = True
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                add_secondary_error(
                    primary, "RPC cancellation also failed", cleanup_error
                )


def complete_execution(
    execution: Execution[_ResponseT],
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
    try:
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
    except BaseException as completion_error:
        if input_error is None:
            raise
        add_secondary_error(
            input_error, "RPC completion also failed", completion_error
        )
        raise input_error from None
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
