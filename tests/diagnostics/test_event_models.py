from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from protodial import cli
from protodial.argument_parser import parse_arguments
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
)
from protodial.rpc_core.models import RpcFailure, RpcSummary


def test_semantic_context_is_strict_at_construction_and_emit() -> None:
    with pytest.raises(ValueError, match="unknown context key.*extra"):
        DiagnosticEvent(
            "rpc.completed",
            fields={
                "status": "OK",
                "sent": 1,
                "received": 1,
                "extra": "ambiguous",
            },
        )
    with pytest.raises(TypeError, match="'sent'.*integer"):
        DiagnosticEvent(
            "rpc.completed",
            fields={"status": "OK", "sent": True, "received": 1},
        )

    context = {"status": "OK", "sent": 1, "received": 1}
    event = DiagnosticEvent("rpc.completed", fields=context)
    context["elapsed_seconds"] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        StderrDiagnosticSink(
            1, stream=io.StringIO(), diagnostic_format="json"
        ).emit(event)


@pytest.mark.parametrize(
    ("event_name", "fields"),
    [
        (
            "rpc.completed",
            {"status": "OK", "sent": -1, "received": 0},
        ),
        (
            "rpc.failed",
            {"elapsed_seconds": -0.01},
        ),
        (
            "rpc.failed",
            {"received_messages": -1},
        ),
        (
            "rpc.lifecycle.completed",
            {"sent": 0, "received": -1},
        ),
    ],
)
def test_known_elapsed_and_counter_fields_reject_negative_values(
    event_name: str, fields: dict[str, object]
) -> None:
    with pytest.raises(TypeError, match="non-negative"):
        DiagnosticEvent(event_name, fields=fields)  # type: ignore[arg-type]


def test_rpc_terminal_machine_context_has_additive_compatible_aliases() -> None:
    command = parse_arguments(["grpc://api.example:50051/example.Users/Create"])
    summary = RpcSummary(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="INVALID_ARGUMENT", value=(3, "unused")),
        details="validation failed",
        sent_messages=1,
        received_messages=0,
        elapsed_seconds=0.5,
    )
    failed = cli._rpc_failure_event(
        command, RpcFailure(Exception("failure"), summary, False)
    )
    failed_stream = io.StringIO()
    StderrDiagnosticSink(
        0, stream=failed_stream, diagnostic_format="json"
    ).emit(failed)
    failed_value = json.loads(failed_stream.getvalue())

    assert failed_value["context"]["status"] == "INVALID_ARGUMENT"
    assert failed_value["context"]["target"] == "api.example:50051"
    assert failed_value["context"]["grpc_status"] == {
        "name": "INVALID_ARGUMENT",
        "number": 3,
    }
    assert failed_value["context"]["sent"] == 1
    assert failed_value["context"]["received"] == 0
    assert failed_value["context"]["sent_messages"] == 1
    assert failed_value["context"]["received_messages"] == 0


def test_rpc_failed_machine_message_is_semantic_when_prose_is_absent() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(
        DiagnosticEvent(
            "rpc.failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            fields={
                "method": "example.Users/Create",
                "grpc_status": {"name": "PERMISSION_DENIED", "number": 7},
            },
        )
    )

    value = json.loads(stream.getvalue())
    assert value["message"] == (
        "RPC `example.Users/Create` failed with PERMISSION_DENIED (7)"
    )
