from __future__ import annotations

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
)

from .terminal_support import TtyBuffer


def test_human_color_policy_and_terminal_injection_safety() -> None:
    event = DiagnosticEvent(
        "command.failed",
        "bad\nforged\rline\x1b]0;owned\x07\u202evalue",
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
    )
    colored = TtyBuffer()
    StderrDiagnosticSink(
        1,
        stream=colored,
        color="always",
        environ={"NO_COLOR": "", "TERM": "dumb"},
    ).emit(event)
    plain = TtyBuffer()
    StderrDiagnosticSink(1, stream=plain, color="never").emit(event)

    assert colored.getvalue().startswith("\x1b[1;31mprotodial: (E2001)\x1b[0m ")
    assert "\x1b]0" not in colored.getvalue()
    assert "\\nforged\\rline\\x1b]0;owned\\x07\\u202evalue" in (
        colored.getvalue()
    )
    assert "\x1b" not in plain.getvalue()


@pytest.mark.parametrize(
    ("environ", "expected_color"),
    [
        ({"TERM": "xterm"}, True),
        ({"TERM": "dumb"}, False),
        ({"TERM": "xterm", "NO_COLOR": ""}, False),
        ({"TERM": "xterm", "NO_COLOR": "1"}, False),
    ],
)
def test_auto_color_uses_diagnostic_tty_and_environment(
    environ: dict[str, str], expected_color: bool
) -> None:
    stream = TtyBuffer()
    StderrDiagnosticSink(0, stream=stream, color="auto", environ=environ).emit(
        DiagnosticEvent(
            "command.failed",
            "failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
        )
    )
    assert ("\x1b[" in stream.getvalue()) is expected_color
