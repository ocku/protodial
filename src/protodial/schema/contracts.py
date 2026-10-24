"""Define schema discovery and reflection contracts."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Protocol, TypeAlias

from google.protobuf import descriptor_pool
from google.protobuf.descriptor import (
    Descriptor,
    EnumDescriptor,
    EnumValueDescriptor,
    FieldDescriptor,
    MethodDescriptor,
    OneofDescriptor,
    ServiceDescriptor,
)
from google.protobuf.message import Message

from ..request_metadata.types import Metadata
from ..rpc_core.protocols import RpcRuntime
from ..transport_core.protocols import GrpcTransport

DescriptorSymbol: TypeAlias = (
    Descriptor
    | EnumDescriptor
    | EnumValueDescriptor
    | FieldDescriptor
    | MethodDescriptor
    | OneofDescriptor
    | ServiceDescriptor
)


class DescriptorSource(Protocol):
    """Provide a descriptor pool and symbol discovery operations."""

    @property
    def pool(self) -> descriptor_pool.DescriptorPool:
        """Return the source's descriptor pool."""
        ...

    def list_services(self) -> Sequence[str]:
        """Return fully qualified service names."""
        ...

    def find_symbol(self, name: str) -> DescriptorSymbol:
        """Resolve a descriptor symbol by name."""
        ...


class ReflectionErrorResponse(Protocol):
    """Describe a reflection protocol error response."""

    error_code: int
    error_message: str


class ReflectionFileResponse(Protocol):
    """Provide serialized file descriptors from reflection."""

    file_descriptor_proto: Sequence[bytes]


class ReflectionService(Protocol):
    """Provide a reflected service name."""

    name: str


class ReflectionServiceResponse(Protocol):
    """Provide reflected service records."""

    service: Sequence[ReflectionService]


class ReflectionExtensionNumberResponse(Protocol):
    """Provide reflected extension numbers."""

    extension_number: Sequence[int]


class ReflectionResponse(Protocol):
    """Describe a server-reflection response message."""

    original_request: Message
    valid_host: str
    error_response: ReflectionErrorResponse
    file_descriptor_response: ReflectionFileResponse
    all_extension_numbers_response: ReflectionExtensionNumberResponse
    list_services_response: ReflectionServiceResponse

    def HasField(self, field_name: str) -> bool:
        """Return whether an optional response field is present."""
        ...

    def WhichOneof(self, oneof_group: str) -> str | None:
        """Return the selected response variant."""
        ...


class ReflectionMessageType(Protocol):
    """Construct and serialize one reflection wire-message type."""

    def __call__(self, **fields: object) -> Message:
        """Construct a reflection message from named fields."""
        ...

    def SerializeToString(self, message: Message) -> bytes:
        """Serialize a reflection message."""
        ...

    def FromString(self, data: bytes) -> Message:
        """Parse a serialized reflection message."""
        ...


class ReflectionWireModule(Protocol):
    """Provide request and response types for one reflection version."""

    ServerReflectionRequest: ReflectionMessageType
    ServerReflectionResponse: ReflectionMessageType


class ReflectionCall(Iterator[Message], Protocol):
    """Iterate reflection responses and support cancellation."""

    def cancel(self) -> object:
        """Cancel the reflection stream."""
        ...


class ReflectionRpc(Protocol):
    """Start a bidirectional reflection stream."""

    def __call__(
        self,
        requests: Iterator[Message],
        *,
        metadata: Metadata,
        timeout: float,
    ) -> ReflectionCall:
        """Start a reflection request stream."""
        ...


class ReflectionChannel(Protocol):
    """Create a dynamic reflection stream callable."""

    def stream_stream(
        self,
        path: str,
        *,
        request_serializer: Callable[[Message], bytes],
        response_deserializer: Callable[[bytes], Message],
    ) -> ReflectionRpc:
        """Create a bidirectional reflection RPC callable."""
        ...


class GrpcModule(GrpcTransport, RpcRuntime, Protocol):
    """grpcio module surface used by transport, reflection, and RPC calls."""


class RpcError(Protocol):
    """Provide a status code for a failed reflection call."""

    def code(self) -> object:
        """Return the reflection call's status code."""
        ...
