import io
import json
import re
import struct
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    message_factory,
    struct_pb2,
)

from protodial.message_io import (
    MessageParseError,
    MessageTooLarge,
    TruncatedFrameBody,
    TruncatedFrameHeader,
    iter_json_requests,
    iter_json_values,
    iter_protobuf_requests,
    iter_raw_requests,
    iter_text_documents,
    iter_text_requests,
    message_to_json,
    render_response,
    write_response,
)
from protodial.rpc import select_bytes_field


def _message_class() -> Any:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="messages_test.proto", package="messages_test", syntax="proto3"
    )
    message = file_proto.message_type.add(name="Payload")
    message.field.add(
        name="display_name",
        json_name="displayName",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    message.field.add(
        name="count",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    )
    message.field.add(
        name="data",
        number=3,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return message_factory.GetMessageClass(
        pool.FindMessageTypeByName("messages_test.Payload")
    ), pool


MESSAGE, POOL = _message_class()


def _oneof_message_class() -> tuple[Any, descriptor_pool.DescriptorPool]:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="raw_oneof_test.proto",
        package="raw_oneof_test",
        syntax="proto3",
    )
    message = file_proto.message_type.add(name="Payload")
    message.oneof_decl.add(name="content")
    message.field.add(
        name="data",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
        oneof_index=0,
    )
    message.field.add(
        name="reference",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        oneof_index=0,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return (
        message_factory.GetMessageClass(
            pool.FindMessageTypeByName("raw_oneof_test.Payload")
        ),
        pool,
    )


ONEOF_MESSAGE, ONEOF_POOL = _oneof_message_class()


def _options(**overrides: Any) -> SimpleNamespace:
    values = {
        "allow_unknown_fields": False,
        "emit_defaults": False,
        "max_input_buffer": None,
        "max_send_message_size": None,
        "output_format": "json",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


class ChunkSource(io.BytesIO):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class Read1Source:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        raise AssertionError("incremental input must use read1")

    def read1(self, _size: int = -1) -> bytes:
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""


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
    with pytest.raises(MessageParseError, match="unknown"):
        next(strict)

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


def test_textproto_unary_and_delimited_stream() -> None:
    stream = io.BytesIO(b'display_name: "one"\n---\ncount: 2\n---\n')
    messages = list(iter_text_requests(stream, True, MESSAGE, POOL, _options()))
    assert messages == [MESSAGE(display_name="one"), MESSAGE(count=2)]

    unary = iter_text_requests(
        io.BytesIO(b"count: 1\n---\ncount: 2\n"),
        False,
        MESSAGE,
        POOL,
        _options(),
    )
    with pytest.raises(MessageParseError, match="more than one document"):
        next(unary)

    assert list(
        iter_text_requests(io.BytesIO(), False, MESSAGE, POOL, _options())
    ) == [MESSAGE()]


def test_text_input_limit_bounds_each_read() -> None:
    class Source(io.BytesIO):
        requested: list[int]

        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.requested = []

        def readline(self, size: int = -1) -> bytes:
            self.requested.append(size)
            return super().readline(size)

    source = Source(b"x" * 20)
    with pytest.raises(MessageTooLarge, match="maximum size of 8 bytes"):
        list(
            iter_text_requests(
                source, True, MESSAGE, POOL, _options(max_input_buffer=8)
            )
        )
    assert source.requested == [9]


def test_text_input_limit_accounts_for_the_retained_document() -> None:
    class Source(io.BytesIO):
        requested: list[int]

        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.requested = []

        def readline(self, size: int = -1) -> bytes:
            self.requested.append(size)
            return super().readline(size)

    source = Source(b"1234\n123\nlong trailing line")
    with pytest.raises(MessageTooLarge, match="maximum size of 10 bytes"):
        list(
            iter_text_requests(
                source, True, MESSAGE, POOL, _options(max_input_buffer=10)
            )
        )

    assert source.requested == [11, 6, 5]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_text_input_accepts_delimiter_after_exact_limit(
    newline: bytes,
) -> None:
    document = b"count: 1" + newline
    delimiter = b"---" + newline

    assert list(
        iter_text_documents(
            io.BytesIO(document + delimiter + delimiter + delimiter),
            _options(max_input_buffer=len(document)),
        )
    ) == [document.decode(), "", ""]


def test_textproto_unknown_field_control() -> None:
    with pytest.raises(MessageParseError, match="unknown"):
        list(
            iter_text_requests(
                io.BytesIO(b"unknown: 1"), False, MESSAGE, POOL, _options()
            )
        )
    assert list(
        iter_text_requests(
            io.BytesIO(b"unknown: 1"),
            False,
            MESSAGE,
            POOL,
            _options(allow_unknown_fields=True),
        )
    ) == [MESSAGE()]


def test_protobuf_stream_framing_and_distinct_truncation_errors() -> None:
    payloads = [
        MESSAGE(count=1).SerializeToString(),
        MESSAGE(count=2).SerializeToString(),
    ]
    framed = b"".join(
        struct.pack(">I", len(payload)) + payload for payload in payloads
    )
    assert list(
        iter_protobuf_requests(io.BytesIO(framed), True, MESSAGE, _options())
    ) == [
        MESSAGE(count=1),
        MESSAGE(count=2),
    ]

    with pytest.raises(TruncatedFrameHeader, match="header"):
        list(
            iter_protobuf_requests(
                io.BytesIO(b"\x00\x00"), True, MESSAGE, _options()
            )
        )
    with pytest.raises(
        TruncatedFrameBody, match=r"expected 3 bytes, received 2"
    ):
        list(
            iter_protobuf_requests(
                io.BytesIO(struct.pack(">I", 3) + b"\x08\x01"),
                True,
                MESSAGE,
                _options(),
            )
        )


def test_protobuf_frame_limit_is_checked_before_body_read() -> None:
    source = io.BytesIO(struct.pack(">I", 100) + b"body must remain unread")
    with pytest.raises(MessageTooLarge, match=r"frame size 100.*8 bytes"):
        next(
            iter_protobuf_requests(
                source,
                True,
                MESSAGE,
                _options(max_input_buffer=10, max_send_message_size=8),
            )
        )
    assert source.tell() == 4


def test_unary_binary_inputs_continue_after_short_reads() -> None:
    raw_source = ChunkSource([b"ab", b"cd", b""])
    assert list(
        iter_raw_requests(
            raw_source,
            False,
            16,
            MESSAGE,
            "data",
            _options(max_input_buffer=16),
        )
    ) == [MESSAGE(data=b"abcd")]

    serialized = MESSAGE(display_name="delayed", count=7).SerializeToString()
    protobuf_source = ChunkSource(
        [serialized[:2], serialized[2:5], serialized[5:], b""]
    )
    assert list(
        iter_protobuf_requests(
            protobuf_source,
            False,
            MESSAGE,
            _options(max_input_buffer=64),
        )
    ) == [MESSAGE(display_name="delayed", count=7)]


def test_raw_chunks_are_lazy_and_validated_before_reading() -> None:
    source = io.BytesIO(b"abcdef")
    with pytest.raises(MessageTooLarge, match=r"chunk size 4.*3 bytes"):
        next(
            iter_raw_requests(
                source,
                True,
                4,
                MESSAGE,
                "data",
                _options(max_send_message_size=3),
            )
        )
    assert source.tell() == 0

    assert list(
        iter_raw_requests(
            io.BytesIO(b"abcde"), True, 3, MESSAGE, "data", _options()
        )
    ) == [
        MESSAGE(data=b"abc"),
        MESSAGE(data=b"de"),
    ]

    short_reads = ChunkSource([b"a", b"b", b"c", b"d", b"e", b""])
    assert list(
        iter_raw_requests(short_reads, True, 3, MESSAGE, "data", _options())
    ) == [
        MESSAGE(data=b"abc"),
        MESSAGE(data=b"de"),
    ]


def test_raw_input_applies_typed_fields_to_every_chunk() -> None:
    requests = iter_raw_requests(
        io.BytesIO(b"abcde"),
        True,
        3,
        MESSAGE,
        "data",
        _options(),
        (("displayName", "video.bin"), ("count", "2")),
        POOL,
    )

    assert list(requests) == [
        MESSAGE(display_name="video.bin", count=2, data=b"abc"),
        MESSAGE(display_name="video.bin", count=2, data=b"de"),
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ((("missing", "value"),), "unknown field"),
        ((("data", "value"),), "conflicts with the file-backed"),
        (
            (("display_name", "one"), ("displayName", "two")),
            "specified more than once",
        ),
        ((("count", "not-a-number"),), "invalid --field value"),
    ],
)
def test_raw_input_rejects_invalid_static_fields(
    fields: tuple[tuple[str, str], ...], message: str
) -> None:
    requests = iter_raw_requests(
        io.BytesIO(b"data"), False, 4, MESSAGE, "data", _options(), fields, POOL
    )

    with pytest.raises(MessageParseError, match=message):
        next(requests)


@pytest.mark.parametrize(
    "value",
    ["NaN", "Infinity", "-Infinity", "1e999", '{"x":1,"x":2}'],
)
def test_raw_field_invalid_or_lossy_json_falls_back_to_string(
    value: str,
) -> None:
    requests = iter_raw_requests(
        io.BytesIO(b"data"),
        False,
        4,
        MESSAGE,
        "data",
        _options(),
        (("displayName", value),),
        POOL,
    )

    assert next(requests) == MESSAGE(display_name=value, data=b"data")


def test_raw_field_integer_conversion_limit_falls_back_to_string() -> None:
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    limit = get_limit() if get_limit is not None else 0
    if limit == 0:
        pytest.skip("Python integer string conversion limit is disabled")
    value = "1" * (limit + 1)
    requests = iter_raw_requests(
        io.BytesIO(b"data"),
        False,
        4,
        MESSAGE,
        "data",
        _options(),
        (("displayName", value),),
        POOL,
    )

    assert next(requests) == MESSAGE(display_name=value, data=b"data")


def test_raw_input_rejects_static_field_in_selected_field_oneof() -> None:
    requests = iter_raw_requests(
        io.BytesIO(b"data"),
        False,
        4,
        ONEOF_MESSAGE,
        "data",
        _options(),
        (("reference", "stored-object"),),
        ONEOF_POOL,
    )

    with pytest.raises(
        MessageParseError,
        match=r"reference.*file-backed bytes field 'data'.*oneof 'content'",
    ):
        next(requests)


def test_json_version_adapter_emits_compact_canonical_json_with_defaults() -> (
    None
):
    rendered = message_to_json(
        MESSAGE(display_name="Ada"),
        POOL,
        _options(emit_defaults=True),
    )
    value = json.loads(rendered)
    assert value == {"displayName": "Ada", "count": 0, "data": ""}
    assert "\n" not in rendered


def test_response_rendering_for_all_formats() -> None:
    message = MESSAGE(display_name="Ada", count=7, data=b"raw")
    json_output = render_response(
        message, _options(output_format="json"), streaming=True, pool=POOL
    )
    assert json.loads(json_output) == {
        "displayName": "Ada",
        "count": 7,
        "data": "cmF3",
    }
    assert json_output.endswith(b"\n")

    text_output = render_response(
        message, _options(output_format="text"), streaming=True, pool=POOL
    )
    assert text_output.endswith(b"\n---\n")
    assert b'display_name: "Ada"' in text_output

    wire = message.SerializeToString()
    assert (
        render_response(
            message, _options(output_format="protobuf"), streaming=True
        )
        == struct.pack(">I", len(wire)) + wire
    )
    assert (
        render_response(
            message, _options(output_format="protobuf"), streaming=False
        )
        == wire
    )
    assert (
        render_response(
            message,
            _options(output_format="raw"),
            streaming=True,
            raw_field="data",
        )
        == b"raw"
    )


def test_write_response_flushes_binary_output_and_handles_partial_writes() -> (
    None
):
    class Output(io.BytesIO):
        flushed = False

        def write(self, data: Any) -> int:
            return super().write(bytes(data[:2]))

        def flush(self) -> None:
            self.flushed = True

    output = Output()
    write_response(
        MESSAGE(data=b"payload"),
        _options(output_format="raw"),
        SimpleNamespace(server_streaming=True),
        POOL,
        "data",
        output,
    )
    assert output.getvalue() == b"payload"
    assert output.flushed
