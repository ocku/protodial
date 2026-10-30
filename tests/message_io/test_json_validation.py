import io
import json
import sys

import pytest

from protodial.message_io import (
    JsonMessageSchemaError,
    MessageParseError,
    MessageTooLarge,
    iter_json_requests,
    iter_json_values,
    iter_protobuf_requests,
    iter_raw_requests,
    iter_text_requests,
)

from .support import MESSAGE, POOL, ChunkSource, _options


def test_all_streaming_formats_accept_zero_messages() -> None:
    assert (
        list(iter_json_requests(io.BytesIO(), True, MESSAGE, POOL, _options()))
        == []
    )
    assert (
        list(iter_text_requests(io.BytesIO(), True, MESSAGE, POOL, _options()))
        == []
    )
    assert (
        list(iter_protobuf_requests(io.BytesIO(), True, MESSAGE, _options()))
        == []
    )
    assert (
        list(
            iter_raw_requests(
                io.BytesIO(), True, 1024, MESSAGE, "data", _options()
            )
        )
        == []
    )


def test_unary_json_validates_trailing_input_before_yielding() -> None:
    requests = iter_json_requests(
        io.BytesIO(b'{"count": 1} trailing'), False, MESSAGE, POOL, _options()
    )
    with pytest.raises(MessageParseError, match="request 2"):
        next(requests)


def test_json_reports_message_and_absolute_line_column() -> None:
    source = io.BytesIO(b'{"count": 1}\n  {"count": }')
    with pytest.raises(
        MessageParseError, match=r"request 2 at line 2, column 13"
    ):
        list(iter_json_values(source, _options()))


def test_json_wraps_integer_conversion_limit_with_source_location() -> None:
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    limit = get_limit() if get_limit is not None else 0
    if limit == 0:
        pytest.skip("Python integer string conversion limit is disabled")
    payload = b"{}\n  " + (b"1" * (limit + 1))

    with pytest.raises(
        MessageParseError,
        match=(
            r"request 2 at line 2, column 3: "
            r"JSON number exceeds the parser limit"
        ),
    ):
        list(
            iter_json_values(
                io.BytesIO(payload),
                _options(max_input_buffer=len(payload) + 1),
            )
        )


def test_json_wraps_recursion_limit_with_source_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = (b"[" * 2000) + b"0" + (b"]" * 2000)
    payload = b"{}\n  " + nested
    raw_decode = json.JSONDecoder.raw_decode

    def decode_with_recursion_limit(
        decoder: json.JSONDecoder, value: str, index: int = 0
    ) -> tuple[object, int]:
        if value.startswith("["):
            raise RecursionError("maximum recursion depth exceeded")
        return raw_decode(decoder, value, index)

    monkeypatch.setattr(
        json.JSONDecoder, "raw_decode", decode_with_recursion_limit
    )

    with pytest.raises(
        MessageParseError,
        match=r"request 2 at line 2, column 3: JSON nesting is too deep",
    ):
        list(
            iter_json_values(
                io.BytesIO(payload),
                _options(max_input_buffer=len(payload) + 1),
            )
        )


def test_json_unknown_field_control_and_buffer_limit() -> None:
    strict = iter_json_requests(
        io.BytesIO(b'{"unknown": true}'), False, MESSAGE, POOL, _options()
    )
    with pytest.raises(JsonMessageSchemaError, match="unknown") as raised:
        next(strict)
    assert raised.value.message_index == 1
    assert raised.value.message_type == "messages_test.Payload"
    assert "no field named" in raised.value.reason
    assert "\n" not in raised.value.reason
    assert "\\n" not in raised.value.reason

    permissive = iter_json_requests(
        io.BytesIO(b'{"unknown": true}'),
        False,
        MESSAGE,
        POOL,
        _options(allow_unknown_fields=True),
    )
    assert next(permissive) == MESSAGE()

    with pytest.raises(MessageTooLarge, match="maximum size of 4 bytes"):
        list(
            iter_json_values(
                io.BytesIO(b'{"count": 1}'), _options(max_input_buffer=4)
            )
        )


@pytest.mark.parametrize(
    "source",
    [
        io.BytesIO(b"{} {}"),
        ChunkSource([b"{}", b" ", b"{}", b""]),
        ChunkSource([b"{} {}", b""]),
    ],
)
def test_json_buffer_limit_is_independent_of_source_chunking(
    source: io.BytesIO,
) -> None:
    assert list(iter_json_values(source, _options(max_input_buffer=2))) == [
        ({}, "{}"),
        ({}, "{}"),
    ]


def test_json_buffer_limit_still_rejects_one_oversized_value() -> None:
    with pytest.raises(MessageTooLarge, match="maximum size of 2 bytes"):
        list(
            iter_json_values(
                ChunkSource([b'{"x": 1}', b""]),
                _options(max_input_buffer=2),
            )
        )
