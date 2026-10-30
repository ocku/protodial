import io

import pytest

from protodial.message_io import (
    MessageParseError,
    MessageTooLarge,
    iter_text_documents,
    iter_text_requests,
)

from .support import MESSAGE, POOL, _options


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
