"""Apply input and message size limits."""

from __future__ import annotations

from typing import BinaryIO

from .models import MessageError, MessageTooLarge, SendLimitOptions


def limit(value: object, name: str) -> int | None:
    """Return a validated nonnegative limit or no limit."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MessageError(f"{name} must be a non-negative integer")
    return value


def effective_message_limit(
    options: SendLimitOptions,
) -> tuple[int | None, str]:
    """Return the strictest configured input or send limit."""
    limits = [
        (value, name)
        for value, name in (
            (
                limit(options.max_input_buffer, "max_input_buffer"),
                "max_input_buffer",
            ),
            (
                limit(options.max_send_message_size, "max_send_message_size"),
                "max_send_message_size",
            ),
        )
        if value is not None
    ]
    if not limits:
        return None, "configured limit"
    return min(limits)


def read_bounded(
    source: BinaryIO, maximum: int | None, description: str
) -> bytes:
    """Read a stream while enforcing an optional byte limit."""
    if maximum is None:
        return source.read()
    data = bytearray()
    bound = maximum + 1
    while len(data) < bound:
        chunk = source.read(bound - len(data))
        if not chunk:
            break
        data.extend(chunk[: bound - len(data)])
    if len(data) > maximum:
        raise MessageTooLarge(
            f"{description} exceeds maximum size of {maximum} bytes"
        )
    return bytes(data)
