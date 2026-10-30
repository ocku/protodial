from __future__ import annotations

import io
import json
import re

from protodial.argument_parser import parse_arguments
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

from .terminal_support import TtyBuffer


def test_presentation_options_and_environment_precedence(monkeypatch) -> None:
    target = "grpc://localhost:50051/example.Service/Call"
    monkeypatch.setenv("PROTODIAL_DIAGNOSTIC_FORMAT", "json")
    from_environment = parse_arguments([target])
    overridden = parse_arguments(
        ["--diagnostic-format", "human", "--color", "never", target]
    )

    assert from_environment.diagnostics.diagnostic_format == "json"
    assert overridden.diagnostics.diagnostic_format == "human"
    assert overridden.diagnostics.color == "never"


def test_json_sink_is_ndjson_sequenced_typed_and_never_colored() -> None:
    stream = TtyBuffer()
    sink = StderrDiagnosticSink(
        verbosity=2,
        stream=stream,
        diagnostic_format="json",
        color="always",
    )
    sink.emit(
        DiagnosticEvent(
            "rpc.headers",
            "response headers received",
            metadata=(
                ("same", "one"),
                ("same", "two"),
                ("trace-bin", b"\x00\xff"),
                ("authorization", "secret"),
                ("ordinary", "literal value"),
            ),
        )
    )
    sink.emit(
        DiagnosticEvent(
            "input.invalid",
            "request contains a newline\nand an escape \x1b[2J",
            severity=DiagnosticSeverity.ERROR,
            code="E2101",
            spans=(DiagnosticSpan("--data", line=1, column=2),),
        )
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["schema"] == "protodial.diagnostic.v1"
    assert [first["sequence"], second["sequence"]] == [1, 2]
    assert [item["name"] for item in first["metadata"]] == [
        "same",
        "same",
        "trace-bin",
        "authorization",
        "ordinary",
    ]
    assert first["metadata"][2]["value"] == {
        "encoding": "base64",
        "data": "AP8=",
    }
    assert first["metadata"][3]["value"] == "secret"
    assert first["metadata"][4]["value"] == "literal value"
    assert second["message"].startswith("request contains a newline\n")
    assert "\x1b[" not in stream.getvalue()


def test_empty_metadata_phases_are_silent_only_for_human_output() -> None:
    events = (
        DiagnosticEvent("rpc.headers", level=0, metadata=()),
        DiagnosticEvent(
            "rpc.trailers",
            level=0,
            metadata=(("blank", " \t"), (" ", "visible")),
        ),
    )
    human_stream = io.StringIO()
    human_sink = StderrDiagnosticSink(0, stream=human_stream, color="never")
    json_stream = io.StringIO()
    json_sink = StderrDiagnosticSink(
        0, stream=json_stream, diagnostic_format="json"
    )

    for event in events:
        human_sink.emit(event)
        json_sink.emit(event)

    assert human_stream.getvalue() == ""
    assert human_sink.sequence == 0
    machine = [json.loads(line) for line in json_stream.getvalue().splitlines()]
    assert [item["event"] for item in machine] == [
        "rpc.headers",
        "rpc.trailers",
    ]
    assert [item["sequence"] for item in machine] == [1, 2]
    assert machine[1]["metadata"] == [
        {"name": "blank", "value": " \t"},
        {"name": " ", "value": "visible"},
    ]


def test_metadata_wire_blocks_wrap_safely_with_stable_continuations() -> None:
    event = DiagnosticEvent(
        "rpc.headers",
        metadata=(
            ("same\x1b[2J界", "first界 value " * 20),
            ("same\x1b[2J界", "second界 value " * 20),
            ("k" * 55, "界 tail"),
            ("trace-bin", b"\x00" * 80),
        ),
    )

    rendered = render_human(event, color=True, width=60)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()

    assert all(_display_width(line) <= 60 for line in lines)
    assert lines[-1] == "<"
    assert all(line.startswith("<") for line in lines)
    assert sum(line.startswith("<   ") for line in lines) >= 2
    assert plain.count(r"same\x1b[2J界:") == 2
    assert (
        plain.index("first") < plain.index("second") < plain.index("trace-bin")
    )
    assert "\x1b" not in plain


def test_extension_event_retains_arbitrary_context() -> None:
    event = DiagnosticEvent(
        "extension.event", "extension message", fields={"answer": 42}
    )
    json_stream = io.StringIO()
    StderrDiagnosticSink(1, stream=json_stream, diagnostic_format="json").emit(
        event
    )

    assert json.loads(json_stream.getvalue())["context"] == {"answer": 42}
