from __future__ import annotations

import io
import json
import re

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
)


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_large_diagnostic_without_details_is_strictly_bounded(
    diagnostic_format: str,
) -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(
        0,
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="always",
    ).emit(
        DiagnosticEvent(
            "command.failed",
            "`" + "wide界e\u0301" * (16 * 1024) + "`",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
        )
    )

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    if diagnostic_format == "json":
        value = json.loads(output)
        assert value["event"] == "command.failed"
        assert "truncated" in value["notes"][0]
    else:
        assert "  note: diagnostic truncated;" in output
        assert "rich status truncated" not in output
        # Every escape introduced by styling is a complete SGR sequence.
        assert re.sub(r"\x1b\[[0-9;]*m", "", output).find("\x1b") == -1


def test_oversized_ordinary_metadata_uses_generic_truncation_wording() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="never").emit(
        DiagnosticEvent(
            "rpc.headers",
            level=0,
            metadata=(("ordinary", "x" * (70 * 1024)),),
        )
    )

    rendered = stream.getvalue()
    assert "  note: diagnostic truncated;" in rendered
    assert "rich status truncated" not in rendered
    assert len(rendered.encode("utf-8")) < 64 * 1024


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_large_rpc_failure_without_details_retains_essentials(
    diagnostic_format: str,
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "localhost:50053",
            "method": "example.Validator/Validate",
            "status": "INVALID_ARGUMENT",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
            "sent": 1,
            "received": 0,
            "sent_messages": 1,
            "received_messages": 0,
        },
        causes=("validation failed " + "x" * (70 * 1024),),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(
        0,
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="always",
    ).emit(event)

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    if diagnostic_format == "json":
        value = json.loads(output)
        assert value["context"]["grpc_status"] == {
            "name": "INVALID_ARGUMENT",
            "number": 3,
        }
        assert value["context"]["method"] == "example.Validator/Validate"
        assert value["causes"][0].startswith("validation failed")
    else:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
        assert "INVALID_ARGUMENT (3)" in plain
        assert "reason: validation failed" in plain
        assert "call: `example.Validator/Validate`" in plain
        assert "localhost:50053" not in plain
        assert re.sub(r"\x1b\[[0-9;]*m", "", output).find("\x1b") == -1


def test_colored_oversized_rpc_never_splits_an_ansi_sequence() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="always").emit(
        DiagnosticEvent(
            "rpc.failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            fields={"grpc_status": {"name": "X" * (70 * 1024), "number": 13}},
        )
    )

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    assert re.sub(r"\x1b\[[0-9;]*m", "", output).find("\x1b") == -1


@pytest.mark.parametrize("color", ["never", "always"])
@pytest.mark.parametrize("detail_kind", ["BadRequest", "DebugInfo"])
def test_large_human_rich_details_retain_essential_failure_context(
    color: str, detail_kind: str
) -> None:
    if detail_kind == "BadRequest":
        detail = {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": "resource", "description": "x" * (70 * 1024)},
            ),
        }
    else:
        detail = {
            "type": "type.googleapis.com/google.rpc.DebugInfo",
            "detail": "x" * (70 * 1024),
            "stack_entries": ("service.py:20",),
        }
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "localhost:50053",
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        causes=("validation failed",),
        details=(detail,),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color=color).emit(event)

    output = stream.getvalue()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    assert len(output) < 12 * 1024
    assert "rich status truncated" in plain
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in plain
    assert "  reason: validation failed" in plain
    assert "  call: `examples.errors.Validator/Validate`" in plain
    assert "localhost:50053" not in plain
    assert (
        "ValidateRequest.resource"
        if detail_kind == "BadRequest"
        else "debug information:"
    ) in plain
    assert (
        output.count("\x1b[") > 0
        if color == "always"
        else "\x1b[" not in output
    )


def test_large_failure_values_cannot_displace_other_essential_sections() -> (
    None
):
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "target-" + "t" * (70 * 1024),
            "method": "method-" + "m" * (70 * 1024),
            "grpc_status": {"name": "INTERNAL", "number": 13},
        },
        causes=("reason-" + "r" * (70 * 1024),),
        details=(
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "debug-" + "d" * (70 * 1024),
            },
        ),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="never").emit(event)

    rendered = stream.getvalue()
    assert len(rendered) < 12 * 1024
    assert "protodial: (E4103) INTERNAL (13)" in rendered
    assert "  reason: reason-" in rendered
    assert "  call: `method-" in rendered
    assert "target-" not in rendered
    assert "rich status truncated" in rendered
