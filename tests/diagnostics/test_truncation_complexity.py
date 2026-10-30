from __future__ import annotations

import json
import re

import pytest

from protodial.presentation._graphemes import (
    grapheme_clusters,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    JsonTruncationMetrics,
    _bounded_diagnostic_json_base,
    _display_width,
    _truncate_json_with_bounded_base,
    render_human,
    truncate_generic_human,
    truncate_rpc_failure_human,
)


@pytest.mark.parametrize("width", [60, 61, 79, 120])
@pytest.mark.parametrize("color", [False, True])
def test_rpc_truncation_tiny_limits_remain_utf8_ansi_and_width_safe(
    width: int, color: bool
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        "界👩‍💻" * 4_000,
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {"field": "resource", "description": "x" * 70_000},
                ),
            },
        ),
    )

    for limit in (1, 2, 7, 31, 63, 127, 511, 4096):
        rendered = truncate_rpc_failure_human(
            event,
            limit,
            color=color,
            width=width,
        )
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

        assert len(rendered.encode("utf-8")) <= limit
        assert rendered.encode("utf-8").decode("utf-8") == rendered
        assert not rendered.endswith("\x1b")
        assert not re.search(r"\x1b\[[0-9;]*$", rendered)
        assert "\x1b" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        assert all(_display_width(line) <= width for line in plain.splitlines())


@pytest.mark.parametrize("color", [False, True])
def test_generic_truncation_tiny_limits_are_utf8_grapheme_and_ansi_safe(
    color: bool,
) -> None:
    glyph = "\U0001f469\u200d\U0001f4bb"
    event = DiagnosticEvent(
        "command.failed",
        "bad " + glyph * 30_000,
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
        help=("FIX_" + glyph,),
    )
    complete = render_human(event, color=color)

    for limit in (1, 2, 7, 31, 63, 127, 511, 4096):
        rendered = truncate_generic_human(
            event,
            complete,
            limit,
            color=color,
            width=60,
        )
        assert len(rendered.encode("utf-8")) <= limit
        assert rendered.encode("utf-8").decode("utf-8") == rendered
        assert not rendered.endswith("\x1b")
        assert not re.search(r"\x1b\[[0-9;]*$", rendered)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        marker = "\n  note: diagnostic truncated; "
        if not color and marker in rendered:
            body, count = rendered.split(marker, 1)
            assert int(count.removesuffix(" characters omitted")) == (
                len(complete) - len(body)
            )
        if glyph in plain:
            assert all(
                cluster != "\U0001f469" for cluster in grapheme_clusters(plain)
            )


def test_bounded_generic_base_reads_only_source_order_context_candidates() -> (
    None
):
    fields = {f"field_{index:04d}": index for index in range(5_000)}
    event = DiagnosticEvent(
        "extension.failed",
        "real message",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E9999",
        fields=fields,
    )
    metrics = JsonTruncationMetrics()

    bounded = _bounded_diagnostic_json_base(
        event, 7, 16 * 1024, _metrics=metrics
    )

    assert bounded["event"] == "extension.failed"
    assert bounded["code"] == "E9999"
    assert bounded["message"] == "real message"
    assert bounded["notes"] == [
        "diagnostic truncated to fit the 64 KiB sink limit"
    ]
    assert list(bounded["context"]) == list(fields)[:8]
    assert metrics.base_candidates == 1
    assert metrics.context_entries == 8


def test_bounded_json_detail_selection_is_linear_for_5k_details() -> None:
    details = tuple(
        (
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "debug-" + "x" * 64,
                "wire_index": index,
            }
            if index % 2 == 0
            else {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": f"field_{index}",
                        "description": "fix this value",
                    },
                ),
                "wire_index": index,
            }
        )
        for index in range(5_000)
    )
    event = DiagnosticEvent(
        "extension.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        details=details,
    )
    metrics = JsonTruncationMetrics()

    byte_limit = 64 * 1024 - 2
    rendered = _truncate_json_with_bounded_base(
        event, 1, byte_limit, _metrics=metrics
    )
    value = json.loads(rendered)
    marker = value["details"][-1]
    survivors = [item["wire_index"] for item in value["details"][:-1]]
    omitted = marker["omitted_detail_indexes"]

    assert len(rendered.encode("utf-8")) <= byte_limit
    assert survivors == sorted(survivors)
    assert survivors and all(index % 2 == 1 for index in survivors)
    assert sorted([*survivors, *omitted]) == list(range(5_000))
    assert marker["original_detail_count"] == 5_000
    remaining = json.dumps(
        [details[index] for index in omitted],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert marker["preview"] == remaining[: len(marker["preview"])]
    assert marker["characters_omitted"] == (
        len(remaining) - len(marker["preview"])
    )
    assert metrics.normalized_details == 5_000
    assert metrics.serialized_details == 5_000
    assert metrics.selection_candidates == 5_000
    assert metrics.final_detail_scans == 5_000
    assert metrics.preview_candidates <= len(remaining).bit_length() + 1
