# pyright: strict, reportUnnecessaryTypeIgnoreComment=true
"""Negative type fixtures for the RPC and transport boundaries."""

from collections.abc import Iterator

from google.protobuf.message import Message

from protodial.commands.models import (
    DiagnosticOptions,
    InvokeOptions,
)
from protodial.request_metadata.types import Metadata
from protodial.rpc_core.call import invoke_rpc
from protodial.rpc_core.protocols import (
    ResponseWriter,
    RpcCall,
    RpcChannel,
    RpcRuntime,
    StreamStreamCallable,
    StreamUnaryCallable,
    UnaryStreamCallable,
    UnaryUnaryCallable,
    UnaryUnaryMethod,
)
from protodial.transport_core.protocols import Channel


def _accept_rpc_channel(channel: RpcChannel) -> None:
    pass


def _accept_transport_channel(channel: Channel) -> None:
    pass


_accept_rpc_channel(object())  # pyright: ignore[reportArgumentType]
_accept_transport_channel(object())  # pyright: ignore[reportArgumentType]


def _incomplete_writer(_message: Message) -> None:
    pass


writer: ResponseWriter[Message] = _incomplete_writer  # pyright: ignore[reportAssignmentType]


class _UnaryFuture:
    def future(self, request: Message, **kwargs: object) -> RpcCall[Message]:
        raise NotImplementedError


class _UnaryStream:
    def __call__(self, request: Message, **kwargs: object) -> RpcCall[Message]:
        raise NotImplementedError


class _StreamFuture:
    def future(
        self, request: Iterator[Message], **kwargs: object
    ) -> RpcCall[Message]:
        raise NotImplementedError


class _StreamStream:
    def __call__(
        self, request: Iterator[Message], **kwargs: object
    ) -> RpcCall[Message]:
        raise NotImplementedError


unary_unary: UnaryUnaryCallable[Message, Message] = _UnaryFuture()
unary_stream: UnaryStreamCallable[Message, Message] = _UnaryStream()
stream_unary: StreamUnaryCallable[Message, Message] = _StreamFuture()
stream_stream: StreamStreamCallable[Message, Message] = _StreamStream()

# Each callable shape rejects an implementation from another cardinality.
bad_unary: UnaryUnaryCallable[Message, Message] = _UnaryStream()  # pyright: ignore[reportAssignmentType]
bad_stream: StreamUnaryCallable[Message, Message] = _UnaryFuture()  # pyright: ignore[reportAssignmentType]


def reject_mismatched_method_pair(
    rpc: UnaryStreamCallable[Message, Message],
    method: UnaryUnaryMethod,
    requests: Iterator[Message],
    options: InvokeOptions,
    metadata: Metadata,
    runtime: RpcRuntime,
    diagnostics: DiagnosticOptions,
    response_writer: ResponseWriter[Message],
) -> None:
    """Prove that callable and method cardinalities cannot disagree."""
    invoke_rpc(  # pyright: ignore[reportCallIssue]
        rpc,
        requests,
        options,
        1.0,
        method,  # pyright: ignore[reportArgumentType]
        metadata,
        None,
        None,
        runtime,
        diagnostics,
        response_writer=response_writer,
    )
