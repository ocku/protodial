import io

import pytest
from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
)

from protodial.message_io import (
    MessageParseError,
    iter_json_requests,
    iter_json_values,
)
from protodial.rpc import select_bytes_field

from .support import MESSAGE, POOL, ChunkSource, Read1Source, _options


def test_raw_bytes_field_selection_accepts_proto_and_json_names() -> None:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="raw_field_name_test.proto",
        package="raw_field_name_test",
        syntax="proto3",
    )
    message = file_proto.message_type.add(name="Payload")
    message.field.add(
        name="raw_payload",
        json_name="rawPayload",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("raw_field_name_test.Payload")

    assert select_bytes_field(descriptor, "raw_payload", "--input-field") == (
        "raw_payload"
    )
    assert select_bytes_field(descriptor, "rawPayload", "--input-field") == (
        "raw_payload"
    )


def test_json_sequence_is_incremental_and_accepts_all_whitespace_framing() -> (
    None
):
    source = ChunkSource(
        [b'{"count": 1}', b"\n", b'{\n"count": 2\n}  ', b'{"count": 3}', b""]
    )
    requests = iter_json_requests(source, True, MESSAGE, POOL, _options())

    assert next(requests).count == 1
    assert source.reads == 1
    assert [message.count for message in requests] == [2, 3]


def test_live_json_yields_a_complete_value_without_waiting_for_more_input() -> (
    None
):
    source = Read1Source([b'{"count": 1}', b"\n", b""])
    requests = iter_json_requests(source, True, MESSAGE, POOL, _options())

    assert next(requests) == MESSAGE(count=1)
    assert source.reads == 1


@pytest.mark.parametrize(
    ("payload", "expected", "raw", "column"),
    [
        (b"{}{}", {}, "{}", 3),
        (b"truex", True, "true", 5),
        (b"[]false", [], "[]", 3),
        (b'"a""b"', "a", '"a"', 4),
        (b"nullx", None, "null", 5),
        (b"1e2x", 100.0, "1e2", 4),
        (b"-1.5false", -1.5, "-1.5", 5),
        (b"1eX", 1, "1", 2),
    ],
)
def test_missing_json_separators_yield_the_same_prefix_for_every_split(
    payload: bytes,
    expected: object,
    raw: str,
    column: int,
) -> None:
    chunkings = [[payload]] + [
        [payload[:split], payload[split:]] for split in range(1, len(payload))
    ]

    for chunks in chunkings:
        values = iter_json_values(ChunkSource(chunks), _options())

        assert next(values) == (expected, raw)
        with pytest.raises(
            MessageParseError,
            match=(
                rf"request 2 at line 1, column {column}: "
                r"JSON values must be separated by whitespace"
            ),
        ):
            next(values)


@pytest.mark.parametrize("separator", ["\u00a0", "\v", "\f", "\u2003"])
def test_json_framing_rejects_non_json_whitespace(separator: str) -> None:
    payload = f"{{}}{separator}{{}}".encode()

    for split in range(1, len(payload) + 1):
        chunks = (
            [payload]
            if split == len(payload)
            else [
                payload[:split],
                payload[split:],
            ]
        )
        values = iter_json_values(ChunkSource(chunks), _options())

        assert next(values) == ({}, "{}")
        with pytest.raises(
            MessageParseError,
            match=(
                r"request 2 at line 1, column 3: "
                r"JSON values must be separated by whitespace"
            ),
        ):
            next(values)


def test_json_framing_accepts_exactly_the_json_whitespace_characters() -> None:
    payload = b"{} \t\r\n[]"

    assert list(iter_json_values(io.BytesIO(payload), _options())) == [
        ({}, "{}"),
        ([], "[]"),
    ]
