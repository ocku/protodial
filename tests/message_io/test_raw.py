import io
import sys

import pytest

from protodial.message_io import (
    MessageParseError,
    MessageTooLarge,
    iter_raw_requests,
)

from .support import (
    MESSAGE,
    ONEOF_MESSAGE,
    ONEOF_POOL,
    POOL,
    ChunkSource,
    _options,
)


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
