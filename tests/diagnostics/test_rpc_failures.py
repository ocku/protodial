from __future__ import annotations

import io
from types import SimpleNamespace

from protodial import cli
from protodial.argument_parser import parse_arguments
from protodial.presentation.diagnostics.error_events import (
    ordinary_error_event,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    render_human,
)
from protodial.rpc_core.models import RpcFailure, RpcSummary


def test_rpc_failure_prioritizes_status_reason_and_compacts_call_context() -> (
    None
):
    command = parse_arguments(["grpc://api.example:50051/example.Users/Create"])
    summary = RpcSummary(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="INVALID_ARGUMENT", value=(3, "unused")),
        details="request validation failed",
        sent_messages=1,
        received_messages=2,
        elapsed_seconds=0.0241,
    )
    event = cli._rpc_failure_event(
        command,
        RpcFailure(Exception("internal transport wrapper"), summary, False),
    )
    rendered = io.StringIO()
    StderrDiagnosticSink(0, stream=rendered, color="never").emit(event)

    assert rendered.getvalue().splitlines() == [
        "protodial: (E4103) INVALID_ARGUMENT (3) in 24.1 ms; "
        "sent 1 message; received 2 messages",
        "  reason: request validation failed",
        "  call: `example.Users/Create`",
    ]


def test_rpc_failure_does_not_render_target_without_a_method() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "api.example:50051",
            "grpc_status": {"name": "UNAVAILABLE", "number": 14},
        },
    )

    assert render_human(event).splitlines() == [
        "protodial: (E4103) UNAVAILABLE (14)"
    ]


def test_rpc_failure_places_rich_details_before_call_context() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        "supplied prose is not the headline",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "localhost:50053",
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
            "elapsed_seconds": 0.00076,
            "sent_messages": 1,
            "received_messages": 0,
        },
        causes=("validation failed",),
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "resource must not be empty",
                    },
                ),
            },
        ),
    )

    assert render_human(event).splitlines() == [
        "ValidateRequest.resource: resource must not be empty",
        "protodial: (E4103) INVALID_ARGUMENT (3) in 760 us; "
        "sent 1 message; received 0 messages",
        "  reason: validation failed",
        "  call: `examples.errors.Validator/Validate`",
    ]


def test_rpc_failure_color_distinguishes_field_label_and_method() -> None:
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
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "resource must not be empty",
                    },
                ),
            },
        ),
    )

    rendered = render_human(event, color=True)

    assert (
        "\x1b[1mValidateRequest.resource\x1b[0m: resource must not be empty"
        in rendered
    )
    assert "\x1b[1mcall\x1b[0m:" in rendered
    assert "\x1b[34m`examples.errors.Validator/Validate`\x1b[0m" in rendered
    assert "\x1b[1m`examples.errors.Validator/Validate`" not in rendered


def test_transport_failure_names_target_and_preserves_actionable_cause() -> (
    None
):
    command = parse_arguments(["grpc://api.example:50051/example.Users/Create"])
    event = ordinary_error_event(
        ConnectionError("connection deadline exceeded"), command
    )
    rendered = io.StringIO()
    StderrDiagnosticSink(0, stream=rendered, color="never").emit(event)

    assert rendered.getvalue().splitlines() == [
        "protodial: (E3002) could not connect to `api.example:50051`",
        "  reason: connection deadline exceeded",
        "  help: check the target, network, TLS, and timeout settings",
    ]
