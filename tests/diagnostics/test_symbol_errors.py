from __future__ import annotations

import pytest

from protodial.argument_parser import parse_arguments
from protodial.errors import CliError
from protodial.presentation.diagnostics.error_events import (
    operation_symbol,
    ordinary_error_event,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSpan,
)


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan")],
)
def test_ndjson_rejects_nonfinite_values_before_serialization(
    value: float,
) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        DiagnosticEvent("extension.event", fields={"value": value})


@pytest.mark.parametrize(
    ("message", "expected_help", "expected_label"),
    [
        (
            "symbol 'Missing' not found; nearby: example.Present",
            "protodial list TARGET",
            "unknown schema symbol",
        ),
        (
            "ambiguous symbol 'Missing'; choose one of: a.Missing, b.Missing",
            "fully qualified symbol",
            "ambiguous schema symbol",
        ),
    ],
)
def test_symbol_errors_point_to_operand_and_offer_actionable_help(
    message: str,
    expected_help: str,
    expected_label: str,
) -> None:
    argv = ("describe", "localhost:50051", "Missing")
    command = parse_arguments(argv)
    event = ordinary_error_event(CliError(message), command, argv)

    assert event.spans == (
        DiagnosticSpan(
            "argument 3",
            text="Missing",
            start=0,
            end=7,
            label=expected_label,
        ),
    )
    assert expected_help in event.help[0]


@pytest.mark.parametrize(
    ("argv", "message", "label", "help_text"),
    [
        (
            ("list", "localhost:50051", "example.Message"),
            "'example.Message' does not name a service",
            "expected a service",
            "protodial list TARGET",
        ),
        (
            ("grpc://localhost:50051/example.Service/Message",),
            "'example.Service/Message' does not name a method",
            "expected a method",
            "SERVICE/METHOD",
        ),
    ],
)
def test_wrong_kind_operands_are_located_schema_errors(
    argv: tuple[str, ...], message: str, label: str, help_text: str
) -> None:
    command = parse_arguments(argv)
    event = ordinary_error_event(CliError(message), command, argv)

    assert event.code == "E2004"
    assert len(event.spans) == 1
    span = event.spans[0]
    assert span.text is not None
    assert span.text[span.start : span.end] == operation_symbol(command)
    assert span.label == label
    assert help_text in event.help[0]
