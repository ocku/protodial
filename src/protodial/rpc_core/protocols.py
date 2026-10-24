"""Focused structural interfaces for RPC orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Literal, Protocol, TypeAlias, TypeVar

from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message

from ..commands.models import InvokeOptions

MessageT = TypeVar("MessageT", contravariant=True)
ResponseT = TypeVar("ResponseT", covariant=True)


class MethodStreaming(Protocol):
    """Describe an RPC method's streaming cardinality."""

    @property
    def client_streaming(self) -> bool:
        """Report whether the request is streaming."""
        ...

    @property
    def server_streaming(self) -> bool:
        """Report whether the response is streaming."""
        ...


class UnaryUnaryMethod(Protocol):
    """Identify a unary-request, unary-response method."""

    @property
    def client_streaming(self) -> Literal[False]:
        """Report that the request is unary."""
        ...

    @property
    def server_streaming(self) -> Literal[False]:
        """Report that the response is unary."""
        ...


class UnaryStreamMethod(Protocol):
    """Identify a unary-request, streaming-response method."""

    @property
    def client_streaming(self) -> Literal[False]:
        """Report that the request is unary."""
        ...

    @property
    def server_streaming(self) -> Literal[True]:
        """Report that the response is streaming."""
        ...


class StreamUnaryMethod(Protocol):
    """Identify a streaming-request, unary-response method."""

    @property
    def client_streaming(self) -> Literal[True]:
        """Report that the request is streaming."""
        ...

    @property
    def server_streaming(self) -> Literal[False]:
        """Report that the response is unary."""
        ...


class StreamStreamMethod(Protocol):
    """Identify a streaming-request, streaming-response method."""

    @property
    def client_streaming(self) -> Literal[True]:
        """Report that the request is streaming."""
        ...

    @property
    def server_streaming(self) -> Literal[True]:
        """Report that the response is streaming."""
        ...


class RpcCall(Protocol[ResponseT]):
    """Expose responses, metadata, status, and cancellation for a call."""

    def __iter__(self) -> Iterator[ResponseT]:
        """Iterate over streamed response messages."""
        ...

    def initial_metadata(
        self,
    ) -> Iterable[tuple[object, str | bytes]] | None:
        """Return response headers when available."""
        ...

    def trailing_metadata(
        self,
    ) -> Iterable[tuple[object, str | bytes]] | None:
        """Return response trailers when available."""
        ...

    def code(self) -> object:
        """Return the final gRPC status code."""
        ...

    def details(self) -> str:
        """Return the final gRPC status detail text."""
        ...

    def result(self) -> ResponseT:
        """Wait for and return a unary response."""
        ...

    def cancel(self) -> object:
        """Cancel the in-flight RPC."""
        ...


class UnaryUnaryCallable(Protocol[MessageT, ResponseT]):
    """Start a unary-request, unary-response RPC."""

    def future(self, request: MessageT, **kwargs: object) -> RpcCall[ResponseT]:
        """Start the RPC and return its call handle."""
        ...


class UnaryStreamCallable(Protocol[MessageT, ResponseT]):
    """Start a unary-request, streaming-response RPC."""

    def __call__(
        self, request: MessageT, **kwargs: object
    ) -> RpcCall[ResponseT]:
        """Start the RPC and return its call handle."""
        ...


class StreamUnaryCallable(Protocol[MessageT, ResponseT]):
    """Start a streaming-request, unary-response RPC."""

    def future(
        self, request: Iterator[MessageT], **kwargs: object
    ) -> RpcCall[ResponseT]:
        """Start the RPC and return its call handle."""
        ...


class StreamStreamCallable(Protocol[MessageT, ResponseT]):
    """Start a bidirectional streaming RPC."""

    def __call__(
        self, request: Iterator[MessageT], **kwargs: object
    ) -> RpcCall[ResponseT]:
        """Start the RPC and return its call handle."""
        ...


RpcCallable: TypeAlias = (
    UnaryUnaryCallable[MessageT, ResponseT]
    | UnaryStreamCallable[MessageT, ResponseT]
    | StreamUnaryCallable[MessageT, ResponseT]
    | StreamStreamCallable[MessageT, ResponseT]
)


class RpcChannel(Protocol):
    """Create dynamic RPC callables for the four cardinalities."""

    def unary_unary(
        self, method: str, **kwargs: object
    ) -> UnaryUnaryCallable[Message, Message]:
        """Create a unary-request, unary-response callable."""
        ...

    def unary_stream(
        self, method: str, **kwargs: object
    ) -> UnaryStreamCallable[Message, Message]:
        """Create a unary-request, streaming-response callable."""
        ...

    def stream_unary(
        self, method: str, **kwargs: object
    ) -> StreamUnaryCallable[Message, Message]:
        """Create a streaming-request, unary-response callable."""
        ...

    def stream_stream(
        self, method: str, **kwargs: object
    ) -> StreamStreamCallable[Message, Message]:
        """Create a bidirectional streaming callable."""
        ...


class StatusCodeValue(Protocol):
    """Expose grpc.StatusCode's numeric and descriptive value."""

    value: tuple[int, str]


class RpcStatusCodes(Protocol):
    """Provide status-code constants used by RPC orchestration."""

    UNKNOWN: object
    NOT_FOUND: StatusCodeValue
    UNIMPLEMENTED: StatusCodeValue


class RpcRuntime(Protocol):
    """Provide the runtime gRPC API required for invocation."""

    RpcError: type[Exception]
    StatusCode: RpcStatusCodes


WrittenResponseT = TypeVar("WrittenResponseT", contravariant=True)


class ResponseWriter(Protocol[WrittenResponseT]):
    """Write one response using configured output options."""

    def __call__(
        self,
        message: WrittenResponseT,
        options: InvokeOptions,
        method: MethodStreaming,
        pool: DescriptorPool | None,
        output_field: str | None,
    ) -> None:
        """Serialize and write one response message."""
        ...
