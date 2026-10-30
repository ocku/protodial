import io
import struct

import pytest

from protodial.message_io import (
    MessageTooLarge,
    TruncatedFrameBody,
    TruncatedFrameHeader,
    iter_protobuf_requests,
    iter_raw_requests,
)

from .support import MESSAGE, ChunkSource, _options


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
