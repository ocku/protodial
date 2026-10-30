from __future__ import annotations

import io
import re

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    _display_width,
    render_human,
)


def test_source_span_expands_tabs_before_positioning_caret() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(1, stream=stream, color="never").emit(
        DiagnosticEvent(
            "input.invalid",
            "invalid input",
            severity=DiagnosticSeverity.ERROR,
            code="E2101",
            spans=(
                DiagnosticSpan(
                    "--data",
                    line=1,
                    column=2,
                    text="\tbad",
                    start=1,
                    end=4,
                    label="invalid token",
                ),
            ),
        )
    )

    source, caret = (
        line for line in stream.getvalue().splitlines() if "| " in line
    )
    assert source.endswith("|     bad")
    assert caret.endswith("|     ^^^ invalid token")


@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("text", ["x" * 100, "界" * 50, "e\u0301" * 100])
def test_source_insertion_at_end_keeps_caret_and_label_within_width(
    color: bool, text: str
) -> None:
    event = DiagnosticEvent(
        "input.invalid",
        "missing value",
        severity=DiagnosticSeverity.ERROR,
        code="E2101",
        spans=(
            DiagnosticSpan(
                "argument 1",
                line=1,
                text=text,
                start=len(text),
                end=len(text),
                label="insert the missing value here",
            ),
        ),
    )

    rendered = render_human(event, color=color, width=60)
    plain_lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", line) for line in rendered.splitlines()
    ]

    assert all(_display_width(line) <= 60 for line in plain_lines)
    caret_line = next(line for line in plain_lines if "^" in line)
    assert caret_line.count("^") == 1
    assert "insert the missing value here" in "\n".join(plain_lines)
