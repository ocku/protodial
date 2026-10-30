from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    render_json,
)


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_large_rich_details_are_bounded_with_explicit_note(
    diagnostic_format: str,
) -> None:
    stream = io.StringIO()
    sink = StderrDiagnosticSink(
        0,
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="always",
    )
    sink.emit(
        DiagnosticEvent(
            "rpc.failed",
            "RPC failed with INTERNAL (13)",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            details=({"type": "example.Huge", "value": "x" * (70 * 1024)},),
        )
    )

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    assert "rich status truncated" in output
    if diagnostic_format == "json":
        value = json.loads(output)
        marker = value["details"][-1]
        assert marker["type"] == "protodial.truncated"
        assert marker["preview"].startswith('[{"type":"example.Huge"')
        original = json.dumps(
            [{"type": "example.Huge", "value": "x" * (70 * 1024)}],
            separators=(",", ":"),
        )
        assert marker["characters_omitted"] == (
            len(original) - len(marker["preview"])
        )
        assert "\x1b" not in output


def test_rich_detail_priority_survives_preceding_bulk_in_human_and_json() -> (
    None
):
    details = (
        {
            "type": "type.googleapis.com/google.rpc.DebugInfo",
            "detail": "debug " + "x" * (70 * 1024),
        },
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {
                    "field": "resource",
                    "description": "resource must not be empty",
                },
            ),
        },
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        causes=("validation failed",),
        details=details,
    )

    # The ordinary JSON API remains a lossless wire-order representation.
    assert [
        item["type"] for item in json.loads(render_json(event, 1))["details"]
    ] == [
        "type.googleapis.com/google.rpc.DebugInfo",
        "type.googleapis.com/google.rpc.BadRequest",
    ]

    human = io.StringIO()
    StderrDiagnosticSink(0, stream=human, color="never").emit(event)
    rendered = human.getvalue()
    assert rendered.index("ValidateRequest.resource") < rendered.index(
        "debug information:"
    )
    assert "INVALID_ARGUMENT (3)" in rendered
    assert "reason: validation failed" in rendered
    assert "call: `examples.errors.Validator/Validate`" in rendered

    machine = io.StringIO()
    StderrDiagnosticSink(0, stream=machine, diagnostic_format="json").emit(
        event
    )
    value = json.loads(machine.getvalue())
    assert value["details"][0]["type"].endswith("google.rpc.BadRequest")
    assert value["details"][0]["field_violations"][0]["field"] == "resource"
    marker = value["details"][-1]
    assert marker["type"] == "protodial.truncated"
    assert marker["omitted_detail_indexes"] == [0]
    assert marker["original_detail_count"] == 2
    assert value["context"]["grpc_status"]["name"] == "INVALID_ARGUMENT"
    assert value["context"]["method"] == "examples.errors.Validator/Validate"
    assert value["causes"] == ["validation failed"]


@pytest.mark.parametrize(
    "oversized_field",
    ["cause", "help", "metadata", "method", "note", "status", "target"],
)
def test_oversized_failure_fields_cannot_displace_actionable_json_detail(
    oversized_field: str,
) -> None:
    huge = "界" * (70 * 1024)
    fields = {
        "target": "localhost:50053",
        "method": "example.Validator/Validate",
        "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
    }
    causes: tuple[str, ...] = ()
    help_items: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    if oversized_field == "cause":
        causes = (huge,)
    elif oversized_field == "help":
        help_items = (huge,)
    elif oversized_field == "metadata":
        metadata = (("large-trailer", huge),)
    elif oversized_field == "method":
        fields["method"] = huge
    elif oversized_field == "note":
        notes = (huge,)
    elif oversized_field == "status":
        fields["grpc_status"] = {"name": huge, "number": 3}
    else:
        fields["target"] = huge

    debug = {
        "type": "type.googleapis.com/google.rpc.DebugInfo",
        "detail": huge,
    }
    bad_request = {
        "type": "type.googleapis.com/google.rpc.BadRequest",
        "field_violations": (
            {"field": "resource", "description": "must not be empty"},
        ),
    }
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields=fields,
        causes=causes,
        help=help_items,
        notes=notes,
        metadata=metadata,
        details=(debug, bad_request),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(event)

    output = stream.getvalue()
    value = json.loads(output)
    assert len(output.encode("utf-8")) < 64 * 1024
    assert value["details"][0] == {
        "type": "type.googleapis.com/google.rpc.BadRequest",
        "field_violations": [
            {"field": "resource", "description": "must not be empty"}
        ],
    }
    marker = value["details"][-1]
    assert marker["type"] == "protodial.truncated"
    assert marker["omitted_detail_indexes"] == [0]
    assert marker["original_detail_count"] == 2
    omitted = json.dumps([debug], ensure_ascii=True, separators=(",", ":"))
    assert marker["preview"] == omitted[: len(marker["preview"])]
    assert marker["characters_omitted"] == len(omitted) - len(marker["preview"])
    assert "method" in value["context"]
    assert "target" in value["context"]
    assert "grpc_status" in value["context"]
    if causes:
        assert value["causes"][0].endswith("…")
    if help_items:
        assert value["help"][0].endswith("…")
    if metadata:
        assert value["metadata"][0]["value"].endswith("…")
    if notes:
        assert value["notes"][0].endswith("…")


def test_json_sink_preserves_exact_content_boundary_then_truncates() -> None:
    details = (
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": "resource", "description": "required"},
            ),
        },
    )
    fields = {
        "target": "localhost:50053",
        "method": "example.Validator/Validate",
        "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
    }
    content_limit = 64 * 1024 - 2
    probe = DiagnosticEvent(
        "rpc.failed",
        "x",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields=fields,
        details=details,
    )
    padding = content_limit - len(render_json(probe, 1))
    exact = replace(probe, message="x" * (padding + 1))
    over = replace(exact, message=exact.message + "x")
    assert len(render_json(exact, 1).encode("utf-8")) == content_limit

    exact_stream = io.StringIO()
    StderrDiagnosticSink(0, stream=exact_stream, diagnostic_format="json").emit(
        exact
    )
    over_stream = io.StringIO()
    StderrDiagnosticSink(0, stream=over_stream, diagnostic_format="json").emit(
        over
    )

    exact_value = json.loads(exact_stream.getvalue())
    over_value = json.loads(over_stream.getvalue())
    assert len(exact_stream.getvalue().encode("utf-8")) == content_limit + 1
    assert "notes" not in exact_value
    assert exact_value["message"] == exact.message
    assert exact_value["details"][0]["type"].endswith("BadRequest")
    assert len(over_stream.getvalue().encode("utf-8")) < 64 * 1024
    assert "truncated" in over_value["notes"][-1]
    assert over_value["details"][0]["type"].endswith("BadRequest")
