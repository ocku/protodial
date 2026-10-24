"""Define request and response message I/O contracts."""

from __future__ import annotations

from typing import Protocol, TypeAlias

from ..errors import CliError
from ..formats import MessageFormat

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class MethodStreaming(Protocol):
    """Describe an RPC method's streaming cardinality."""

    client_streaming: bool
    server_streaming: bool


class InputBufferOptions(Protocol):
    """Provide the maximum retained input size."""

    max_input_buffer: int | None


class SendLimitOptions(InputBufferOptions, Protocol):
    """Provide input and outbound message limits."""

    max_send_message_size: int | None


class JsonInputOptions(InputBufferOptions, Protocol):
    """Provide JSON request parsing options."""

    allow_unknown_fields: bool


class TextInputOptions(InputBufferOptions, Protocol):
    """Provide text-format request parsing options."""

    allow_unknown_fields: bool


class RawInputOptions(SendLimitOptions, Protocol):
    """Provide raw request construction options."""

    fields: tuple[tuple[str, str], ...]


class RequestOptions(
    JsonInputOptions, TextInputOptions, RawInputOptions, Protocol
):
    """Provide all request input options."""

    data: str | None
    input_format: MessageFormat
    chunk_size: int


class JsonOutputOptions(Protocol):
    """Provide JSON response serialization options."""

    emit_defaults: bool


class OutputOptions(JsonOutputOptions, Protocol):
    """Provide response serialization options."""

    output_format: MessageFormat


class MessageError(CliError):
    """Invalid message input or output configuration."""


class MessageParseError(MessageError):
    """A request message could not be parsed."""


class MessageTooLarge(MessageError):
    """A request exceeds a configured input or send limit."""


class TruncatedFrameHeader(MessageParseError):
    """A protobuf stream ended partway through a frame header."""


class TruncatedFrameBody(MessageParseError):
    """A protobuf stream ended partway through a frame body."""
