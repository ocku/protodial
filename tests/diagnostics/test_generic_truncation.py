from __future__ import annotations

import re

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    render_human,
    truncate_generic_human,
)


@pytest.mark.parametrize("color", [False, True])
def test_generic_truncation_preserves_span_action_and_later_help(
    color: bool,
) -> None:
    event = DiagnosticEvent(
        "command.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
        spans=(
            DiagnosticSpan(
                "request.json",
                text="x" + "\u0301" * 70_000,
                start=0,
                end=1,
                label="MUST_SURVIVE_ACTION",
            ),
        ),
        help=("FIX_NOW",),
    )
    complete = render_human(event, color=color)
    rendered = truncate_generic_human(
        event,
        complete,
        64 * 1024 - 2,
        color=color,
        width=100,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert "1 | …" in plain
    assert "^ MUST_SURVIVE_ACTION" in plain
    assert "help: FIX_NOW" in plain
    assert "diagnostic truncated" in plain
    assert len(rendered.encode("utf-8")) <= 64 * 1024 - 2
    assert "\x1b" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered)


def test_generic_truncation_counts_omissions_and_keeps_help() -> None:
    event = DiagnosticEvent(
        "command.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
        causes=("x" * 70_000,),
        help=("FIX_NOW",),
    )
    complete = render_human(event)
    rendered = truncate_generic_human(
        event,
        complete,
        64 * 1024 - 2,
        color=False,
        width=100,
    )
    marker = "\n  note: diagnostic truncated; "
    body, count = rendered.split(marker, 1)

    assert "help: FIX_NOW" in body
    assert int(count.removesuffix(" characters omitted")) == (
        len(complete) - len(body)
    )


def test_generic_truncation_preserves_block_order_past_old_numeric_ranges() -> (
    None
):
    event = DiagnosticEvent(
        "extension.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E9999",
        spans=tuple(
            DiagnosticSpan(
                f"source_{index:03d}",
                text="bad",
                start=0,
                end=3,
                label=f"SPAN_{index:03d}",
            )
            for index in range(101)
        ),
        causes=tuple(f"CAUSE_{index:03d}" for index in range(101)),
        help=tuple(f"HELP_{index:03d}" for index in range(101)),
        notes=tuple(f"NOTE_{index:03d}" for index in range(101)),
        metadata=tuple(
            (f"meta_{index:03d}", f"value_{index:03d}") for index in range(101)
        ),
        details=(
            *(
                {
                    "type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": f"DETAIL_{index:03d}",
                }
                for index in range(101)
            ),
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "x" * 70_000,
            },
        ),
    )
    complete = render_human(event, width=100)
    rendered = truncate_generic_human(
        event, complete, 64 * 1024 - 2, color=False, width=100
    )

    assert "diagnostic truncated" in rendered
    assert rendered.index("SPAN_100") < rendered.index("reason: CAUSE_000")
    assert rendered.index("reason: CAUSE_100") < rendered.index(
        "help: HELP_000"
    )
    assert rendered.index("help: HELP_100") < rendered.index("note: NOTE_000")
    assert rendered.index("note: NOTE_100") < rendered.index("* metadata")
    assert rendered.index("meta_100") < rendered.index("DETAIL_000")
    assert rendered.index("DETAIL_000") < rendered.index("DETAIL_100")
