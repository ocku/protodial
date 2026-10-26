"""Parse JSON request messages from bounded input streams."""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterator
from typing import BinaryIO, TypeVar, cast

from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message

from .json_decoding import (
    NonFiniteNumber,
    decode_json_float,
    reject_json_constant,
)
from .limits import limit
from .models import (
    JsonInputOptions,
    JsonMessageSchemaError,
    JsonValue,
    MessageParseError,
    MessageTooLarge,
)

JSON_READ_SIZE = 64 * 1024
_JSON_WHITESPACE = " \t\r\n"

_MessageT = TypeVar("_MessageT", bound=Message)


def _position_after(text: str, line: int, column: int) -> tuple[int, int]:
    newlines = text.count("\n")
    if not newlines:
        return line, column + len(text)
    return line + newlines, len(text) - text.rfind("\n")


def _json_error(
    message_index: int, line: int, column: int, detail: str
) -> MessageParseError:
    return MessageParseError(
        f"invalid JSON request {message_index} at line {line}, column "
        f"{column}: "
        f"{detail}"
    )


def _is_incomplete_json_number(text: str) -> bool:
    """Return whether all of *text* is a valid, incomplete JSON number."""

    def is_digit(character: str) -> bool:
        return "0" <= character <= "9"

    length = len(text)
    index = 0
    if index < length and text[index] == "-":
        index += 1
    if index == length:
        return True

    if text[index] == "0":
        index += 1
    elif "1" <= text[index] <= "9":
        index += 1
        while index < length and is_digit(text[index]):
            index += 1
    else:
        return False

    if index < length and text[index] == ".":
        index += 1
        fraction_start = index
        while index < length and is_digit(text[index]):
            index += 1
        if index == fraction_start:
            return index == length

    if index < length and text[index] in "eE":
        index += 1
        if index < length and text[index] in "+-":
            index += 1
        exponent_start = index
        while index < length and is_digit(text[index]):
            index += 1
        if index == exponent_start:
            return index == length

    return False


class _JsonValueReader:
    """Track framing and source position for an incremental JSON sequence."""

    def __init__(self, source: BinaryIO, max_buffer: int | None) -> None:
        self.source = source
        self.max_buffer = max_buffer
        self.utf8_decoder = codecs.getincrementaldecoder("utf-8")()
        self.json_decoder = json.JSONDecoder(
            parse_float=decode_json_float,
            parse_constant=reject_json_constant,
        )
        self.buffer = ""
        self.eof = False
        self.pending_utf8_error: tuple[int, int, UnicodeDecodeError] | None = (
            None
        )
        self.separator_required = False
        self.line = 1
        self.column = 1
        self.message_index = 1

    def __iter__(self) -> Iterator[tuple[JsonValue, str]]:
        """Yield framed JSON values until the byte stream ends."""
        while self._prepare_value():
            decoded = self._decode_value()
            if decoded is None:
                continue
            value, end = decoded
            raw_value = self.buffer[:end]
            yield value, raw_value
            self._consume_value(raw_value, end)

    def _prepare_value(self) -> bool:
        """Fill and consume framing whitespace before the next value."""
        while True:
            self._fill_until_data()
            if not self.buffer and self.pending_utf8_error is not None:
                self._raise_pending_utf8_error()
            if not self.buffer and self.eof:
                return False
            if (
                self.separator_required
                and self.buffer[0] not in _JSON_WHITESPACE
            ):
                self._raise_separator_error(self.line, self.column)
            if not self._consume_whitespace():
                return True

    def _fill_until_data(self) -> None:
        """Read until decoded text is available or EOF is reached."""
        while not self.buffer and not self.eof:
            self._fill()

    def _consume_whitespace(self) -> bool:
        """Consume leading whitespace and report whether any was found."""
        length = len(self.buffer) - len(self.buffer.lstrip(_JSON_WHITESPACE))
        if not length:
            return False
        self._advance(self.buffer[:length])
        self.buffer = self.buffer[length:]
        self.separator_required = False
        return True

    def _decode_value(self) -> tuple[JsonValue, int] | None:
        """Decode one buffered value, filling when it may be incomplete."""
        start_line, start_column = self.line, self.column
        try:
            value, end = self.json_decoder.raw_decode(self.buffer)
        except NonFiniteNumber as error:
            if self._non_finite_number_may_continue(error):
                self._check_buffer_size()
                self._fill()
                return None
            raise _json_error(
                self.message_index,
                start_line,
                start_column,
                f"non-finite number {error.token!r} is not valid JSON",
            ) from error
        except json.JSONDecodeError as error:
            if self.pending_utf8_error is not None:
                self._check_buffer_size()
                self._raise_pending_utf8_error()
            if not self.eof:
                # A read may return bytes beyond the pending value. Only an
                # input which is still incomplete at this point is retained
                # input for that value and therefore subject to the bound.
                self._check_buffer_size()
                self._fill()
                return None
            error_line = start_line + error.lineno - 1
            error_column = (
                error.colno
                if error.lineno > 1
                else start_column + error.colno - 1
            )
            raise _json_error(
                self.message_index,
                error_line,
                error_column,
                error.msg,
            ) from error
        except RecursionError as error:
            self._check_buffer_size()
            raise _json_error(
                self.message_index,
                start_line,
                start_column,
                "JSON nesting is too deep",
            ) from error
        except ValueError as error:
            self._check_buffer_size()
            raise _json_error(
                self.message_index,
                start_line,
                start_column,
                "JSON number exceeds the parser limit",
            ) from error

        self._check_value_size(end)
        if self._number_may_continue(value, end):
            self._fill()
            return None
        if end < len(self.buffer) and self.buffer[end] not in _JSON_WHITESPACE:
            if _is_incomplete_json_number(self.buffer):
                if self.pending_utf8_error is not None:
                    self._check_buffer_size()
                    self._raise_pending_utf8_error()
                if self.eof:
                    return cast(JsonValue, value), end
                self._check_buffer_size()
                self._fill()
                return None
        return cast(JsonValue, value), end

    def _number_may_continue(self, value: object, end: int) -> bool:
        """Return whether a number needs one more byte of framing."""
        return (
            end == len(self.buffer)
            and not self.eof
            and self.pending_utf8_error is None
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )

    def _non_finite_number_may_continue(self, error: NonFiniteNumber) -> bool:
        """Return whether an overflowed token may extend in the source."""
        return (
            error.may_extend
            and self.buffer.endswith(error.token)
            and not self.eof
            and self.pending_utf8_error is None
            and not self._has_pending_utf8_input()
        )

    def _has_pending_utf8_input(self) -> bool:
        """Return whether the incremental decoder retains an incomplete rune."""
        undecoded, _state = self.utf8_decoder.getstate()
        return bool(undecoded)

    def _consume_value(self, raw_value: str, end: int) -> None:
        """Advance parser state past a decoded value."""
        self._advance(raw_value)
        self.buffer = self.buffer[end:]
        self.separator_required = True
        self.message_index += 1

    def _advance(self, text: str) -> None:
        """Advance the absolute line and column past text."""
        self.line, self.column = _position_after(text, self.line, self.column)

    def _fill(self) -> None:
        """Read and decode one bounded byte chunk."""
        if self.eof:
            return
        read_size = self._next_read_size()
        read = getattr(self.source, "read1", self.source.read)
        chunk = read(read_size)
        try:
            self.buffer += self.utf8_decoder.decode(chunk, final=not chunk)
        except UnicodeDecodeError as error:
            valid_prefix = error.object[: error.start].decode("utf-8")
            error_line, error_column = _position_after(
                self.buffer + valid_prefix, self.line, self.column
            )
            self.buffer += valid_prefix
            # The decoder keeps its pending input when decoding fails. That
            # input is already represented by valid_prefix, so retaining both
            # would count the same bytes twice against max_buffer.
            self.utf8_decoder.reset()
            self.pending_utf8_error = (error_line, error_column, error)
            self.eof = True
            return
        self.eof = not chunk

    def _raise_pending_utf8_error(self) -> None:
        """Raise a deferred decoding error after preceding values are read."""
        pending = self.pending_utf8_error
        if pending is None:  # pragma: no cover - guarded by callers
            raise AssertionError("no pending UTF-8 error")
        line, column, error = pending
        raise _json_error(
            self.message_index,
            line,
            column,
            "invalid UTF-8",
        ) from error

    def _next_read_size(self) -> int:
        """Return a read size that detects a one-byte buffer overflow."""
        if self.max_buffer is None:
            return JSON_READ_SIZE
        remaining = self.max_buffer - self._buffered_bytes()
        read_size = min(JSON_READ_SIZE, remaining + 1)
        if read_size <= 0:
            self._raise_buffer_too_large()
        return read_size

    def _check_buffer_size(self) -> None:
        """Reject retained input larger than the configured bound."""
        if (
            self.max_buffer is not None
            and self._buffered_bytes() > self.max_buffer
        ):
            self._raise_buffer_too_large()

    def _check_value_size(self, end: int) -> None:
        """Reject a decoded pending value larger than the configured bound."""
        if (
            self.max_buffer is not None
            and len(self.buffer[:end].encode("utf-8")) > self.max_buffer
        ):
            self._raise_buffer_too_large()

    def _buffered_bytes(self) -> int:
        """Return the UTF-8 byte size of retained decoded input."""
        undecoded, _state = self.utf8_decoder.getstate()
        return len(self.buffer.encode("utf-8")) + len(undecoded)

    def _raise_buffer_too_large(self) -> None:
        """Raise the shared input-buffer limit error."""
        raise MessageTooLarge(
            f"JSON input buffer exceeds maximum size of {self.max_buffer} bytes"
        )

    def _raise_separator_error(self, line: int, column: int) -> None:
        """Raise a framing error at an absolute source position."""
        raise _json_error(
            self.message_index,
            line,
            column,
            "JSON values must be separated by whitespace",
        )


def iter_json_values(
    source: BinaryIO, options: JsonInputOptions
) -> Iterator[tuple[JsonValue, str]]:
    """Incrementally yield framed JSON values and their original text."""
    max_buffer = limit(options.max_input_buffer, "max_input_buffer")
    yield from _JsonValueReader(source, max_buffer)


def _parse_json_message(
    text: str,
    message_class: type[_MessageT],
    pool: DescriptorPool,
    options: JsonInputOptions,
    message_index: int,
) -> _MessageT:
    from google.protobuf import json_format

    try:
        return json_format.Parse(
            text,
            message_class(),
            ignore_unknown_fields=options.allow_unknown_fields,
            descriptor_pool=pool,
        )
    except (ValueError, TypeError, json_format.ParseError) as error:
        reason = " ".join(str(error).replace("\\n", " ").split())
        raise JsonMessageSchemaError(
            message_index,
            message_class.DESCRIPTOR.full_name,
            reason,
        ) from error


def iter_json_requests(
    source: BinaryIO,
    streaming: bool,
    message_class: type[_MessageT],
    pool: DescriptorPool,
    options: JsonInputOptions,
) -> Iterator[_MessageT]:
    """Yield request messages parsed from a JSON input stream."""
    values = iter_json_values(source, options)
    first = next(values, None)
    if first is None:
        if streaming:
            return
        raise MessageParseError("JSON request input is empty")
    first_message = _parse_json_message(
        first[1], message_class, pool, options, 1
    )

    if not streaming:
        if next(values, None) is not None:
            raise MessageParseError(
                "unary JSON input contains more than one value"
            )
        yield first_message
        return

    yield first_message
    second = next(values, None)
    if second is not None:
        yield _parse_json_message(second[1], message_class, pool, options, 2)
    for index, (_value, text) in enumerate(values, 3):
        yield _parse_json_message(text, message_class, pool, options, index)
