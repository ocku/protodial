"""Build request messages from raw byte input."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, TypeVar

from google.protobuf import json_format
from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message

from .json_decoding import strict_json_loads
from .limits import effective_message_limit, read_bounded
from .models import (
    MessageError,
    MessageParseError,
    MessageTooLarge,
    RawInputOptions,
)

_MessageT = TypeVar("_MessageT", bound=Message)


def _parse_field_value(value: str) -> object:
    try:
        return strict_json_loads(value)
    except (ValueError, RecursionError):
        return value


def _field_template(
    message_class: type[_MessageT],
    raw_field: str,
    assignments: tuple[tuple[str, str], ...],
    pool: DescriptorPool | None,
) -> _MessageT:
    descriptor = message_class.DESCRIPTOR
    selected_field = descriptor.fields_by_name[raw_field]
    values: dict[str, object] = {}
    seen: set[str] = set()
    for supplied_name, supplied_value in assignments:
        field = next(
            (
                candidate
                for candidate in descriptor.fields
                if supplied_name in (candidate.name, candidate.json_name)
            ),
            None,
        )
        if field is None:
            raise MessageParseError(
                f"--field names unknown field {supplied_name!r} on "
                f"{descriptor.full_name}"
            )
        if field.name == raw_field:
            raise MessageParseError(
                f"--field {supplied_name!r} conflicts with the file-backed "
                "bytes field"
            )
        if (
            selected_field.containing_oneof is not None
            and field.containing_oneof is selected_field.containing_oneof
        ):
            raise MessageParseError(
                f"--field {supplied_name!r} conflicts with file-backed bytes "
                f"field {raw_field!r}: both belong to oneof "
                f"{selected_field.containing_oneof.name!r}"
            )
        if field.name in seen:
            raise MessageParseError(
                f"--field {supplied_name!r} is specified more than once"
            )
        seen.add(field.name)
        values[field.name] = _parse_field_value(supplied_value)

    message = message_class()
    if not values:
        return message
    try:
        json_format.ParseDict(values, message, descriptor_pool=pool)
    except (json_format.ParseError, TypeError, ValueError) as error:
        raise MessageParseError(f"invalid --field value: {error}") from error
    return message


def iter_raw_requests(
    source: BinaryIO,
    streaming: bool,
    chunk_size: int,
    message_class: type[_MessageT],
    field_name: str,
    options: RawInputOptions,
    fields: tuple[tuple[str, str], ...] = (),
    pool: DescriptorPool | None = None,
) -> Iterator[_MessageT]:
    """Yield messages whose selected bytes field contains raw input."""
    if isinstance(chunk_size, bool) or chunk_size <= 0:
        raise MessageError("raw chunk size must be a positive integer")
    template = _field_template(message_class, field_name, fields, pool)

    def make_message(payload: bytes) -> _MessageT:
        message = message_class()
        message.CopyFrom(template)
        setattr(message, field_name, payload)
        return message

    maximum, limit_name = effective_message_limit(options)
    if streaming and maximum is not None and chunk_size > maximum:
        raise MessageTooLarge(
            f"raw chunk size {chunk_size} exceeds {limit_name} of "
            f"{maximum} bytes"
        )
    if not streaming:
        yield make_message(read_bounded(source, maximum, "raw request"))
        return
    while True:
        chunk = bytearray()
        while len(chunk) < chunk_size:
            part = source.read(chunk_size - len(chunk))
            if not part:
                break
            chunk.extend(part)
        if not chunk:
            return
        yield make_message(bytes(chunk))
