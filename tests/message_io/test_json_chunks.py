import io

import pytest

from protodial.message_io import (
    MessageParseError,
    MessageTooLarge,
    iter_json_values,
)

from .support import ChunkSource, _options


@pytest.mark.parametrize(
    ("payload", "prefix", "request_index", "line", "column"),
    [
        (b"{}\xff", [({}, "{}")], 2, 1, 3),
        (b"{}\n  []\xff", [({}, "{}"), ([], "[]")], 3, 2, 5),
        ('"\u00e5"'.encode() + b"\xff", [("\u00e5", '"\u00e5"')], 2, 1, 4),
        (b'{"count": \xff', [], 1, 1, 11),
        (b"1e\xff", [], 1, 1, 3),
    ],
)
def test_invalid_utf8_preserves_values_before_it_for_every_split(
    payload: bytes,
    prefix: list[tuple[object, str]],
    request_index: int,
    line: int,
    column: int,
) -> None:
    chunkings = [[payload]] + [
        [payload[:split], payload[split:]] for split in range(1, len(payload))
    ]

    for chunks in chunkings:
        values = iter_json_values(ChunkSource(chunks), _options())

        assert [next(values) for _ in prefix] == prefix
        with pytest.raises(
            MessageParseError,
            match=(
                rf"request {request_index} at line {line}, column {column}: "
                r"invalid UTF-8"
            ),
        ):
            next(values)


def test_incomplete_utf8_at_eof_is_deferred_past_a_complete_value() -> None:
    values = iter_json_values(
        ChunkSource([b"{}\xc3", b""]),
        _options(max_input_buffer=2),
    )

    assert next(values) == ({}, "{}")
    with pytest.raises(
        MessageParseError,
        match=r"request 2 at line 1, column 3: invalid UTF-8",
    ):
        next(values)


def test_valid_prefix_before_invalid_utf8_still_obeys_buffer_limit() -> None:
    values = iter_json_values(
        ChunkSource([b'{"x"\xff']),
        _options(max_input_buffer=3),
    )

    with pytest.raises(MessageTooLarge, match="maximum size of 3 bytes"):
        next(values)


def test_invalid_utf8_at_buffer_limit_is_independent_of_partitions() -> None:
    payload = b'"\xc3\xa0\xa5'
    partitions = []
    for boundary_mask in range(1 << (len(payload) - 1)):
        boundaries = [0]
        boundaries.extend(
            index
            for index in range(1, len(payload))
            if boundary_mask & (1 << (index - 1))
        )
        boundaries.append(len(payload))
        partitions.append(
            [
                payload[start:end]
                for start, end in zip(boundaries, boundaries[1:])
            ]
        )

    for chunks in partitions:
        values = iter_json_values(
            ChunkSource(chunks),
            _options(max_input_buffer=3),
        )

        with pytest.raises(
            MessageParseError,
            match=r"request 1 at line 1, column 3: invalid UTF-8",
        ):
            next(values)


def test_deferred_utf8_error_does_not_read_past_the_failing_chunk() -> None:
    source = ChunkSource([b"{}\xff", b"unread"])
    values = iter_json_values(source, _options(max_input_buffer=2))

    assert next(values) == ({}, "{}")
    assert source.reads == 1
    with pytest.raises(MessageParseError, match="invalid UTF-8"):
        next(values)
    assert source.reads == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1e2", 100.0),
        ("1E2", 100.0),
        ("1e+2", 100.0),
        ("1E-2", 0.01),
        ("1.5", 1.5),
        ("-1.5e+2", -150.0),
        ("0.5E-2", 0.005),
    ],
)
def test_incremental_json_numbers_are_independent_of_chunk_boundaries(
    text: str, expected: float
) -> None:
    encoded = text.encode()
    for split in range(1, len(encoded)):
        source = ChunkSource([encoded[:split], encoded[split:] + b" ", b""])

        assert list(iter_json_values(source, _options())) == [(expected, text)]


def test_framed_json_number_does_not_read_another_chunk() -> None:
    source = ChunkSource([b"1e+2 ", b"unread"])
    values = iter_json_values(source, _options())

    assert next(values) == (100.0, "1e+2")
    assert source.reads == 1


def test_incremental_json_number_preserves_limits_and_separator_errors() -> (
    None
):
    assert list(
        iter_json_values(
            ChunkSource([b"1e", b"2 ", b""]),
            _options(max_input_buffer=3),
        )
    ) == [(100.0, "1e2")]

    with pytest.raises(MessageTooLarge, match="maximum size of 2 bytes"):
        list(
            iter_json_values(
                ChunkSource([b"1e", b"2 ", b""]),
                _options(max_input_buffer=2),
            )
        )
    with pytest.raises(MessageParseError, match="separated by whitespace"):
        list(iter_json_values(io.BytesIO(b"1e2x"), _options()))
