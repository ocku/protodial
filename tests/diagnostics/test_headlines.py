from __future__ import annotations

import io
import json
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


def test_verbose_human_events_render_all_semantic_context() -> None:
    events = (
        DiagnosticEvent(
            "transport.connecting",
            level=2,
            fields={"target": "api.test:443", "transport": "verified"},
        ),
        DiagnosticEvent(
            "rpc.resolved",
            level=1,
            fields={
                "method": "example.Chat/Watch",
                "cardinality": "unary-stream",
                "request_type": "example.WatchRequest",
                "response_type": "example.WatchResponse",
            },
        ),
        DiagnosticEvent(
            "rpc.started",
            level=2,
            fields={"method": "example.Chat/Watch", "compression": "gzip"},
        ),
        DiagnosticEvent(
            "rpc.lifecycle.completed",
            "RPC lifecycle completed",
            level=2,
            fields={"sent": 1, "received": 3},
        ),
    )
    stream = io.StringIO()
    sink = StderrDiagnosticSink(2, stream=stream, color="never")
    for event in events:
        sink.emit(event)

    rendered = stream.getvalue()
    assert "using verified transport" in rendered
    assert "unary-stream" in rendered
    assert "`example.WatchRequest`" in rendered
    assert "`example.WatchResponse`" in rendered
    assert "using gzip compression" in rendered
    assert "sent 1 message; received 3 messages" in rendered


def test_unknown_rich_detail_keeps_its_decode_warning_in_human_and_json() -> (
    None
):
    event = DiagnosticEvent(
        "rpc.failed",
        "RPC failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        details=(
            {
                "type": "type.example/Unknown",
                "value": b"opaque",
                "warning": "unknown message type",
            },
        ),
    )
    human = io.StringIO()
    StderrDiagnosticSink(0, stream=human, color="never").emit(event)
    machine = io.StringIO()
    StderrDiagnosticSink(0, stream=machine, diagnostic_format="json").emit(
        event
    )

    assert (
        "`type.example/Unknown` detail: unknown message type"
        in human.getvalue()
    )
    assert json.loads(machine.getvalue())["details"][0]["warning"] == (
        "unknown message type"
    )


def test_location_only_span_has_one_empty_gutter_not_two() -> None:
    rendered = io.StringIO()
    StderrDiagnosticSink(0, stream=rendered, color="never").emit(
        DiagnosticEvent(
            "command.failed",
            "failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
            spans=(DiagnosticSpan("config.proto:7"),),
        )
    )

    assert rendered.getvalue().splitlines() == [
        "protodial: (E2001) failed",
        "  --> config.proto:7",
        "   |",
    ]


def test_color_styles_identifiers_in_all_sections() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        "RPC `example.Call` failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        causes=("server rejected `request.id`",),
        help=("inspect `request.id`",),
        details=(
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "DENIED",
                "domain": "auth.test",
                "metadata": {"client_secret": "hidden"},
            },
        ),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="always").emit(event)
    rendered = stream.getvalue()

    assert "\x1b[1m`request.id`\x1b[0m" in rendered
    assert "\x1b[1m`DENIED`\x1b[0m" in rendered
    assert "\x1b[1m`auth.test`\x1b[0m" in rendered
    assert "client_secret: hidden" in rendered


@pytest.mark.parametrize(
    "severity", [DiagnosticSeverity.ERROR, DiagnosticSeverity.WARNING]
)
@pytest.mark.parametrize("color", [False, True])
def test_human_headline_sanitizes_diagnostic_code(
    severity: DiagnosticSeverity, color: bool
) -> None:
    malicious_code = "E0000\n\x1b[31mforged"
    rendered = render_human(
        DiagnosticEvent(
            "extension.event",
            "safe headline",
            severity=severity,
            code=malicious_code,
        ),
        color=color,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert plain.count("\n") == 0
    assert "E0000\\n\\x1b[31mforged" in plain
    assert "\x1b[31mforged" not in rendered


@pytest.mark.parametrize(
    "severity", [DiagnosticSeverity.ERROR, DiagnosticSeverity.WARNING]
)
@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("width", [60, 79, 120])
def test_long_extension_code_uses_cell_safe_stacked_headline(
    severity: DiagnosticSeverity, color: bool, width: int
) -> None:
    code = "界" * 80 + "\n\x1b[2J" + "e\u0301" * 40
    rendered = render_human(
        DiagnosticEvent(
            "extension.event",
            "concise actionable headline",
            severity=severity,
            code=code,
        ),
        color=color,
        width=width,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()

    assert all(_display_width(line) <= width for line in lines)
    assert lines[-1] == "  concise actionable headline"
    assert max(len(line) - len(line.lstrip()) for line in lines) <= 2
    assert r"\n\x1b[2J" in plain
    assert "\x1b[2J" not in rendered
    assert "\x1b" not in plain


@pytest.mark.parametrize(
    ("severity", "code", "expected"),
    [
        (
            DiagnosticSeverity.ERROR,
            "E1234",
            "protodial: (E1234) concise headline",
        ),
        (
            DiagnosticSeverity.WARNING,
            "W1234",
            "* warning (W1234): concise headline",
        ),
    ],
)
def test_standard_diagnostic_code_headline_layout_is_unchanged(
    severity: DiagnosticSeverity, code: str, expected: str
) -> None:
    rendered = render_human(
        DiagnosticEvent(
            "extension.event",
            "concise headline",
            severity=severity,
            code=code,
        ),
        width=60,
    )

    assert rendered == expected
