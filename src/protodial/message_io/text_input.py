"""Parse text-format protobuf request messages."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, TypeVar

from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message

from .limits import limit
from .models import MessageParseError, MessageTooLarge, TextInputOptions

TEXT_STREAM_DELIMITER = "---"
_MAX_DELIMITER_LINE_BYTES = len(TEXT_STREAM_DELIMITER.encode("ascii")) + 2

_MessageT = TypeVar("_MessageT", bound=Message)


def _parse_text_message(
    text: str,
    message_class: type[_MessageT],
    pool: DescriptorPool,
    options: TextInputOptions,
    message_index: int,
) -> _MessageT:
    from google.protobuf import text_format

    try:
        return text_format.Parse(
            text,
            message_class(),
            allow_unknown_field=options.allow_unknown_fields,
            descriptor_pool=pool,
        )
    except (ValueError, TypeError, text_format.ParseError) as error:
        raise MessageParseError(
            f"invalid text request {message_index}: {error}"
        ) from error


def iter_text_documents(
    source: BinaryIO, options: TextInputOptions
) -> Iterator[str]:
    """Yield UTF-8 textproto documents separated by a ``---`` line."""

    max_buffer = limit(options.max_input_buffer, "max_input_buffer")
    document = bytearray()
    while True:
        line = (
            source.readline()
            if max_buffer is None
            else source.readline(
                max(
                    max_buffer - len(document) + 1,
                    _MAX_DELIMITER_LINE_BYTES,
                )
            )
        )
        if not line:
            break
        if line.rstrip(b"\r\n") == TEXT_STREAM_DELIMITER.encode("ascii"):
            try:
                yield document.decode("utf-8")
            except UnicodeDecodeError as error:
                raise MessageParseError(
                    "text request contains invalid UTF-8"
                ) from error
            document.clear()
            continue
        if max_buffer is not None and len(document) + len(line) > max_buffer:
            raise MessageTooLarge(
                f"text input buffer exceeds maximum size of {max_buffer} bytes"
            )
        document.extend(line)
    if document.strip():
        try:
            yield document.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MessageParseError(
                "text request contains invalid UTF-8"
            ) from error


def iter_text_requests(
    source: BinaryIO,
    streaming: bool,
    message_class: type[_MessageT],
    pool: DescriptorPool,
    options: TextInputOptions,
) -> Iterator[_MessageT]:
    """Yield requests parsed from protobuf text-format input."""
    documents = iter_text_documents(source, options)
    first = next(documents, None)
    if first is None:
        if streaming:
            return
        first = ""
    first_message = _parse_text_message(first, message_class, pool, options, 1)

    if not streaming:
        if next(documents, None) is not None:
            raise MessageParseError(
                "unary text input contains more than one document"
            )
        yield first_message
        return

    yield first_message
    second = next(documents, None)
    if second is not None:
        yield _parse_text_message(second, message_class, pool, options, 2)
    for index, document in enumerate(documents, 3):
        yield _parse_text_message(document, message_class, pool, options, index)
