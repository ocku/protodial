from __future__ import annotations

import io
import json

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    _display_width,
    render_human,
)


def test_human_semantic_visibility_filters_only_empty_values() -> None:
    details = (
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": "  ", "description": "\t"},
                {"field": "resource", "description": "   "},
                {"field": "\t", "description": "must not be empty"},
            ),
        },
        {
            "type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": " ",
            "domain": "\t",
            "metadata": {" ": "visible", "empty": "\n"},
        },
        {
            "type": "type.googleapis.com/google.rpc.Help",
            "links": (
                {"description": "  ", "url": "https://example.test/docs"},
                {"description": "read the guide", "url": "\t"},
                {"description": " ", "url": ""},
            ),
        },
        {
            "type": "type.googleapis.com/google.rpc.LocalizedMessage",
            "locale": "  ",
            "message": "visible localized message",
        },
        {
            "type": "type.googleapis.com/google.rpc.DebugInfo",
            "detail": "\t",
            "stack_entries": ("  ",),
        },
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        metadata=(("blank", " \t"), (" ", "visible")),
        causes=(" \t",),
        help=("\n",),
        notes=("  ",),
        details=details,
    )

    human = render_human(event)
    machine_stream = io.StringIO()
    StderrDiagnosticSink(
        0, stream=machine_stream, diagnostic_format="json"
    ).emit(event)
    machine = json.loads(machine_stream.getvalue())

    assert "request: invalid" in human
    assert "resource: invalid" in human
    assert "request: must not be empty" in human
    assert "error information:" not in human
    assert "debug information:" not in human
    assert "https://example.test/docs" in human
    assert "read the guide" in human
    assert "localized message:" in human
    assert "localized message (" not in human
    assert "  reason:" not in human
    assert "  note:" not in human
    assert "* metadata" not in human
    assert machine["metadata"][0]["value"] == " \t"
    assert machine["causes"] == [" \t"]
    assert machine["details"][0]["field_violations"][0]["field"] == "  "


def test_custom_short_name_lookalikes_are_unknown_and_lossless() -> None:
    custom_names = (
        "BadRequest",
        "DebugInfo",
        "ErrorInfo",
        "Help",
        "LocalizedMessage",
        "PreconditionFailure",
        "QuotaFailure",
        "RequestInfo",
        "ResourceInfo",
        "RetryInfo",
    )
    custom_details = tuple(
        {
            "type": f"type.googleapis.com/acme.{name}",
            "custom_value": f"MUST_SURVIVE_{name}",
        }
        for name in custom_names
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        details=(
            *custom_details,
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "STANDARD_DEBUG",
            },
        ),
    )

    rendered = render_human(event)

    assert rendered.index("debug information:") < rendered.index(
        "type.googleapis.com/acme.BadRequest:"
    )
    for name in custom_names:
        assert f"type.googleapis.com/acme.{name}:" in rendered
        assert f"custom_value: MUST_SURVIVE_{name}" in rendered
    assert "STANDARD_DEBUG" in rendered


def test_unknown_detail_renders_every_payload_sibling_once_in_input_order() -> (
    None
):
    event = DiagnosticEvent(
        "extension.failed",
        level=0,
        details=(
            {
                "type": "type.example/Everything",
                "title": "custom detail",
                "items": {"z": 1, "a": "two"},
                "value": b"opaque",
                "custom_value": "MUST_SURVIVE",
                "warning": "decode warning",
            },
        ),
    )

    rendered = render_human(event, width=32)

    assert rendered.count("custom detail:") == 1
    assert rendered.count("type.example/Everything") == 1
    assert rendered.count("items:") == 1
    assert rendered.count("value:") == 2  # value + custom_value
    assert rendered.count("decode warning") == 1
    assert "base64:b3BhcXVl" in rendered
    assert "MUST_SURVIVE" in rendered
    assert rendered.index("items:") < rendered.index("  value:")
    assert rendered.index("  value:") < rendered.index("custom_value:")
    assert all(_display_width(line) <= 32 for line in rendered.splitlines())
