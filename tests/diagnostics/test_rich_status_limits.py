from __future__ import annotations

import io
import re

import pytest
from google.rpc import error_details_pb2

from protodial.presentation._graphemes import grapheme_boundaries
from protodial.presentation._truncation import PrefixSearchMetrics
from protodial.rich_status import (
    decode_status_details,
    render_rich_status,
    write_rich_status,
)
from protodial.rich_status import decoding as status_decoding
from protodial.rich_status.rendering import _truncate_rendered

from .status_support import _decoded_detail, _status_bytes


def test_rich_status_truncation_is_bounded_and_counts_original_characters() -> (
    None
):
    decoded = decode_status_details(_status_bytes(message="x" * 200))
    assert decoded is not None
    full = render_rich_status(decoded)

    limited = render_rich_status(decoded, max_rendered_chars=80)
    assert len(limited) == 80
    marker_start = limited.index("\n  note: rich status truncated;")
    match = re.search(r"(\d+) characters omitted$", limited)
    assert match is not None
    assert int(match.group(1)) == len(full) - marker_start

    assert len(render_rich_status(decoded, max_rendered_chars=1)) == 1
    with pytest.raises(ValueError, match="greater than zero"):
        render_rich_status(decoded, max_rendered_chars=0)
    with pytest.raises(ValueError, match="greater than zero"):
        render_rich_status(decoded, max_rendered_chars=-1)


def test_rich_status_limit_is_utf8_bytes_without_details() -> None:
    rendered = render_rich_status(
        None,
        transport_code=3,
        transport_message="界" * 100,
        max_rendered_chars=80,
    )

    assert len(rendered.encode("utf-8")) <= 80
    assert "rich status truncated" in rendered
    assert "\ufffd" not in rendered


@pytest.mark.parametrize(
    "glyph",
    [
        "\U0001f1f8\U0001f1ea",
        "1\ufe0f\u20e3",
        "\u0915\u093f",
        "e\u0301",
        "\U0001f469\u200d\U0001f4bb",
        "\U0001f44d\U0001f3fd",
    ],
)
def test_standalone_rich_status_truncates_at_grapheme_boundaries(
    glyph: str,
) -> None:
    status = status_decoding.RichStatus(3, glyph * 40, ())
    full = render_rich_status(status, transport_code=3)
    boundaries = set(grapheme_boundaries(full))
    observed_truncation = False
    for limit in range(64, min(220, len(full.encode("utf-8")))):
        truncated = render_rich_status(
            status, transport_code=3, max_rendered_chars=limit
        )
        marker = "\n  note: rich status truncated;"
        if marker not in truncated:
            continue
        observed_truncation = True
        prefix = truncated.split(marker, 1)[0]
        assert len(prefix) in boundaries
        assert len(truncated.encode("utf-8")) <= limit
    assert observed_truncation


def test_standalone_truncation_indexes_large_multibyte_value_once() -> None:
    glyph = "\U0001f1f8\U0001f1ea"
    original = "status: " + glyph * 40_000
    metrics = PrefixSearchMetrics()

    rendered = _truncate_rendered(original, 4096, _metrics=metrics)

    assert metrics.scanned_codepoints == len(original)
    assert metrics.safe_boundaries <= 40_009
    assert metrics.candidate_probes <= metrics.safe_boundaries.bit_length() + 1
    assert len(rendered.encode("utf-8")) <= 4096
    assert "rich status truncated" in rendered
    assert "\ufffd" not in rendered


def test_rich_status_limit_is_utf8_bytes_with_details() -> None:
    status = status_decoding.RichStatus(
        3,
        "invalid",
        (_decoded_detail(error_details_pb2.DebugInfo(detail="界" * 1_000)),),
    )

    rendered = render_rich_status(
        status, transport_code=3, max_rendered_chars=240
    )

    assert len(rendered.encode("utf-8")) <= 240
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in rendered
    assert "rich status truncated" in rendered
    assert "\ufffd" not in rendered


def test_large_standalone_rich_status_retains_status_reason_and_preview() -> (
    None
):
    status = status_decoding.RichStatus(
        3,
        "validation failed",
        (
            _decoded_detail(
                error_details_pb2.DebugInfo(detail="x" * (70 * 1024))
            ),
        ),
    )

    rendered = render_rich_status(status, transport_code=3)

    assert len(rendered) <= 64 * 1024
    assert "debug information:" in rendered
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in rendered
    assert "  reason: validation failed" in rendered
    assert "rich status truncated" in rendered


def test_standalone_rich_status_renders_each_detail_warning_once() -> None:
    detail = status_decoding.DecodedStatusDetail(
        "type.example/Unknown",
        "type.example.Unknown",
        b"opaque",
        warning="unknown message type",
    )
    status = status_decoding.RichStatus(
        3,
        "invalid",
        (detail,),
        warnings=("status payload had contextual mismatch",),
    )

    rendered = render_rich_status(status, transport_code=3)

    assert rendered.count("unknown message type") == 1
    assert rendered.count("status payload had contextual mismatch") == 1


def test_status_limits_and_stderr_writer() -> None:
    too_large = decode_status_details(b"12345", max_status_bytes=4)
    assert too_large is not None
    assert too_large.decoded is False
    assert "limit is 4" in too_large.warnings[0]

    decoded = decode_status_details(
        _status_bytes(("unknown.Type", b"abcdef")), max_detail_bytes=3
    )
    assert decoded is not None
    assert "limit is 3" in (decoded.details[0].warning or "")

    stream = io.StringIO()
    write_rich_status(
        decoded, stream=stream, transport_code=3, transport_message="bad"
    )
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in stream.getvalue()
    assert "  reason: bad" in stream.getvalue()


@pytest.mark.parametrize("limit", [1, 2, 17, 80, 256])
def test_rich_status_writer_includes_newline_in_utf8_byte_limit(
    limit: int,
) -> None:
    status = status_decoding.RichStatus(3, "界" * 200, ())
    stream = io.StringIO()

    write_rich_status(
        status,
        stream=stream,
        transport_code=3,
        max_rendered_chars=limit,
    )

    written = stream.getvalue()
    assert written.endswith("\n")
    assert len(written.encode("utf-8")) <= limit
    assert (written == "\n") is (limit == 1)


@pytest.mark.parametrize("limit", [0, -1])
def test_rich_status_writer_rejects_nonpositive_limits_without_writing(
    limit: int,
) -> None:
    stream = io.StringIO()

    with pytest.raises(ValueError, match="greater than zero"):
        write_rich_status(None, stream=stream, max_rendered_chars=limit)

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "name",
    [
        "max_status_bytes",
        "max_detail_bytes",
        "max_details",
        "max_recursion_depth",
    ],
)
@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_status_decode_limits_reject_invalid_values_before_input(
    name: str, value: object
) -> None:
    with pytest.raises(
        ValueError, match=rf"{name} must be a nonnegative integer"
    ):
        decode_status_details(None, **{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "max_status_bytes",
        "max_detail_bytes",
        "max_details",
        "max_recursion_depth",
    ],
)
@pytest.mark.parametrize("value", [0, 1])
def test_status_decode_limits_accept_nonnegative_integers(
    name: str, value: int
) -> None:
    assert decode_status_details(None, **{name: value}) is None
