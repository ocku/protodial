"""Serialize and write gRPC response messages."""

from __future__ import annotations

import struct
import sys
from typing import BinaryIO, cast

from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message

from ..formats import MessageFormat
from .models import (
    JsonOutputOptions,
    MessageError,
    MethodStreaming,
    OutputOptions,
)


def message_to_json(
    message: Message, pool: DescriptorPool | None, options: JsonOutputOptions
) -> str:
    """Render ProtoJSON across protobuf default-field emission versions."""

    from google.protobuf import json_format

    return json_format.MessageToJson(
        message,
        descriptor_pool=pool,
        indent=None,
        always_print_fields_with_no_presence=options.emit_defaults,
    )


def render_response(
    message: Message,
    options: OutputOptions,
    *,
    streaming: bool,
    pool: DescriptorPool | None = None,
    raw_field: str | None = None,
) -> bytes:
    """Render one response, including the selected stream framing."""

    output_format = options.output_format
    if output_format == MessageFormat.JSON:
        return message_to_json(message, pool, options).encode("utf-8") + b"\n"
    if output_format == MessageFormat.TEXT:
        from google.protobuf import text_format

        rendered = text_format.MessageToString(message, descriptor_pool=pool)
        data = rendered.rstrip("\n").encode("utf-8") + b"\n"
        return data + (b"---\n" if streaming else b"")
    if output_format == MessageFormat.PROTOBUF:
        data = message.SerializeToString()
        return (struct.pack(">I", len(data)) if streaming else b"") + data
    if output_format == MessageFormat.RAW:
        if raw_field is None:
            raise MessageError("raw output requires a selected bytes field")
        data = getattr(message, raw_field)
        if not isinstance(data, bytes):
            raise MessageError(f"raw output field {raw_field!r} is not bytes")
        return data
    raise MessageError(f"unsupported output format: {output_format}")


def write_response(
    message: Message,
    options: OutputOptions,
    method: MethodStreaming,
    pool: DescriptorPool,
    raw_field: str | None,
    output: BinaryIO | None = None,
) -> None:
    """Render, write, and flush one response to a binary stream."""

    destination = (
        output if output is not None else cast(BinaryIO, sys.stdout.buffer)
    )
    rendered = render_response(
        message,
        options,
        streaming=bool(method.server_streaming),
        pool=pool,
        raw_field=raw_field,
    )
    remaining = memoryview(rendered)
    while remaining:
        written = destination.write(remaining)
        if written <= 0:
            raise OSError("response output write made no progress")
        remaining = remaining[written:]
    destination.flush()
