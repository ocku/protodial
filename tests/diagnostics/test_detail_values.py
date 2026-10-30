from __future__ import annotations

import io
import json

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    format_duration,
    render_human,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.000999499, "999 us"),
        (0.0009995, "1 ms"),
        (0.999499, "999 ms"),
        (0.9995, "1 s"),
    ],
)
def test_duration_rounding_promotes_normalized_unit_boundaries(
    seconds: float, expected: str
) -> None:
    rendered = format_duration(seconds)

    assert rendered == expected
    assert "e+03" not in rendered


@pytest.mark.parametrize(
    "seconds", [-1.0, float("inf"), float("-inf"), float("nan")]
)
def test_duration_rejects_invalid_values_without_crashing(
    seconds: float,
) -> None:
    assert format_duration(seconds) == "unknown duration"


@pytest.mark.parametrize(
    ("delay", "expected"),
    [
        ("-5s", "unknown duration"),
        ("NaNs", "unknown duration"),
        ("after maintenance", "after maintenance"),
    ],
)
def test_retry_info_has_sane_invalid_and_arbitrary_text(
    delay: str, expected: str
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "UNAVAILABLE", "number": 14}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.RetryInfo",
                "retry_delay": delay,
            },
        ),
    )

    assert f"retry after {expected}" in render_human(event)


def test_nested_rich_detail_values_are_preserved_in_both_renderers() -> None:
    event = DiagnosticEvent(
        "extension.failed",
        "RPC failed with UNAUTHENTICATED (16)",
        severity=DiagnosticSeverity.ERROR,
        fields={"nested": {"password": "field secret"}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "TOKEN_EXPIRED",
                "domain": "example.test",
                "metadata": {
                    "authorization": "detail secret",
                    "ordinary": "visible",
                    "nested": {"api-token": "deep secret"},
                },
            },
        ),
    )
    human = io.StringIO()
    StderrDiagnosticSink(1, stream=human, color="never").emit(event)
    machine = io.StringIO()
    StderrDiagnosticSink(1, stream=machine, diagnostic_format="json").emit(
        event
    )

    rendered = human.getvalue()
    assert "error information:" in rendered
    assert "detail secret" in rendered
    assert "deep secret" in rendered
    assert "ordinary: visible" in rendered

    value = json.loads(machine.getvalue())
    assert value["context"]["nested"]["password"] == "field secret"
    metadata = value["details"][0]["metadata"]
    assert metadata["authorization"] == "detail secret"
    assert metadata["nested"]["api-token"] == "deep secret"
    assert metadata["ordinary"] == "visible"


def test_recursive_rich_detail_preserves_common_key_spellings() -> None:
    secrets = {
        "client_secret": "one",
        "api.token": "two",
        "access_token": "three",
        "db_password": "four",
        "passwordHash": "five",
        "credentials": "six",
    }
    event = DiagnosticEvent(
        "rpc.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        details=(
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "metadata": {"nested": secrets},
            },
        ),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(event)
    nested = json.loads(stream.getvalue())["details"][0]["metadata"]["nested"]

    assert nested == secrets
