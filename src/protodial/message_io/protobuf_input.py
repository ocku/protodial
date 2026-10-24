"""Parse length-delimited protobuf request messages."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import BinaryIO, TypeVar

from google.protobuf.message import DecodeError, Message

from .limits import effective_message_limit, read_bounded
from .models import (
    MessageParseError,
    SendLimitOptions,
    TruncatedFrameBody,
    TruncatedFrameHeader,
)

_FRAME_HEADER_SIZE = 4

_MessageT = TypeVar("_MessageT", bound=Message)


def _read_frame_header(source: BinaryIO) -> bytes | None:
    header = source.read(_FRAME_HEADER_SIZE)
    if not header:
        return None
    while len(header) < _FRAME_HEADER_SIZE:
        chunk = source.read(_FRAME_HEADER_SIZE - len(header))
        if not chunk:
            raise TruncatedFrameHeader(
                "truncated protobuf frame header: expected 4 bytes, received "
                f"{len(header)}"
            )
        header += chunk
    return header


def _read_frame_body(source: BinaryIO, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = source.read(size - len(body))
        if not chunk:
            raise TruncatedFrameBody(
                f"truncated protobuf frame body: expected {size} bytes, "
                f"received {len(body)}"
            )
        body.extend(chunk)
    return bytes(body)


def iter_protobuf_requests(
    source: BinaryIO,
    streaming: bool,
    message_class: type[_MessageT],
    options: SendLimitOptions,
) -> Iterator[_MessageT]:
    """Yield requests parsed from unary or framed protobuf input."""
    maximum, limit_name = effective_message_limit(options)
    if not streaming:
        payload = read_bounded(source, maximum, "protobuf request")
        try:
            yield message_class.FromString(payload)
        except DecodeError as error:
            raise MessageParseError(
                f"invalid protobuf request: {error}"
            ) from error
        return

    message_index = 1
    while (header := _read_frame_header(source)) is not None:
        size = struct.unpack(">I", header)[0]
        if maximum is not None and size > maximum:
            from .models import MessageTooLarge

            raise MessageTooLarge(
                f"protobuf request {message_index} frame size {size} exceeds "
                f"{limit_name} of {maximum} bytes"
            )
        payload = _read_frame_body(source, size)
        try:
            yield message_class.FromString(payload)
        except DecodeError as error:
            raise MessageParseError(
                f"invalid protobuf request {message_index}: {error}"
            ) from error
        message_index += 1
