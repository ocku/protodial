import io
import json
import re
from typing import Any

import pytest
from google.protobuf import (
    descriptor_pool,
    struct_pb2,
)

from protodial.message_io import (
    MessageParseError,
    MessageTooLarge,
    iter_json_requests,
    iter_json_values,
)

from .support import MESSAGE, POOL, ChunkSource, _options


@pytest.mark.parametrize(
    "payload",
    [
        b'{"count": 1}\n{"count": 2}\n',
        b'{\n  "count": 1\n}\n{\n  "count": 2\n}',
        b'{"count": 1} \t {"count": 2}',
    ],
)
def test_json_sequence_forms(payload: bytes) -> None:
    assert [
        value
        for value, _text in iter_json_values(io.BytesIO(payload), _options())
    ] == [
        {"count": 1},
        {"count": 2},
    ]


def test_json_requires_separation_and_unary_requires_one_value() -> None:
    with pytest.raises(MessageParseError, match="separated by whitespace"):
        list(iter_json_values(io.BytesIO(b"{}{}"), _options()))
    requests = iter_json_requests(
        io.BytesIO(b"{} {}"), False, MESSAGE, POOL, _options()
    )
    with pytest.raises(MessageParseError, match="more than one value"):
        next(requests)

    empty = iter_json_requests(io.BytesIO(), False, MESSAGE, POOL, _options())
    with pytest.raises(MessageParseError, match="input is empty"):
        next(empty)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": -Infinity}',
        b'{"value": 1e999}',
    ],
)
@pytest.mark.parametrize("message_class", [struct_pb2.Struct, struct_pb2.Value])
def test_json_rejects_non_finite_numbers_before_wkt_conversion(
    payload: bytes, message_class: Any
) -> None:
    requests = iter_json_requests(
        io.BytesIO(payload),
        False,
        message_class,
        descriptor_pool.Default(),
        _options(),
    )

    with pytest.raises(MessageParseError, match="non-finite number"):
        next(requests)


@pytest.mark.parametrize(
    "token",
    ["1e3099", "-1e3099", "1E+3099", "-1E+3099"],
)
def test_non_finite_json_number_diagnostic_is_partition_independent(
    token: str,
) -> None:
    payload = (token + " ").encode()
    for boundary_mask in range(1 << (len(payload) - 1)):
        boundaries = [0]
        boundaries.extend(
            index
            for index in range(1, len(payload))
            if boundary_mask & (1 << (index - 1))
        )
        boundaries.append(len(payload))
        chunks = [
            payload[start:end] for start, end in zip(boundaries, boundaries[1:])
        ]
        values = iter_json_values(ChunkSource(chunks), _options())

        with pytest.raises(
            MessageParseError,
            match=(
                rf"non-finite number {re.escape(repr(token))} "
                r"is not valid JSON"
            ),
        ):
            next(values)


def test_framed_non_finite_json_number_does_not_wait_for_another_chunk() -> (
    None
):
    source = ChunkSource([b"1e3099 ", b"unread"])
    values = iter_json_values(source, _options())

    with pytest.raises(MessageParseError, match=repr("1e3099")):
        next(values)
    assert source.reads == 1


def test_pending_utf8_frames_non_finite_json_number_without_another_read() -> (
    None
):
    source = ChunkSource([b"1e3099\xc3", b"\xa5"])
    values = iter_json_values(source, _options())

    with pytest.raises(MessageParseError, match=repr("1e3099")):
        next(values)
    assert source.reads == 1


def test_extending_non_finite_json_number_obeys_buffer_limit() -> None:
    values = iter_json_values(
        ChunkSource([b"1e309", b"9"]),
        _options(max_input_buffer=5),
    )

    with pytest.raises(MessageTooLarge, match="maximum size of 5 bytes"):
        next(values)


@pytest.mark.parametrize("value", [0.0, -1.25, 1e308])
def test_json_accepts_finite_wkt_numbers(value: float) -> None:
    payload = json.dumps({"value": value}).encode()
    requests = iter_json_requests(
        io.BytesIO(payload),
        False,
        struct_pb2.Struct,
        descriptor_pool.Default(),
        _options(),
    )

    assert next(requests)["value"] == value
