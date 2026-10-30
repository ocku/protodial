import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import protodial.rpc as rpc_module
from protodial.commands.models import DiagnosticOptions
from protodial.presentation.diagnostics.context import use_sink
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    RecordingDiagnosticSink,
)
from protodial.presentation.diagnostics.rendering import StderrDiagnosticSink
from protodial.presentation.rpc import render_metadata
from protodial.rpc import invoke_rpc
from protodial.rpc_core.call import (
    write_initial_diagnostics,
    write_terminal_diagnostics,
)

from .support import (
    ALL_DIAGNOSTICS,
    FAKE_GRPC,
    OK,
    TEST_OPTIONS,
    FakeCall,
    FakeRpc,
    run_rpc,
)


def test_metadata_rendering_preserves_duplicates_order_and_binary_values() -> (
    None
):
    assert render_metadata(
        [("same", "first"), ("data-bin", b"\x00\xff"), ("same", "second")]
    ) == [
        "same: first",
        "data-bin: base64:AP8=",
        "same: second",
    ]


def test_response_diagnostics_follow_wire_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []

    class TimelineSink:
        def emit(self, event: DiagnosticEvent) -> None:
            event.validate()
            timeline.append(event.name)

    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: timeline.append(str(message)),
    )
    method = SimpleNamespace(client_streaming=False, server_streaming=True)

    with use_sink(TimelineSink()):
        invoke_rpc(
            FakeRpc(FakeCall(["body-1", "body-2"]), False, True),
            iter(["request"]),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            ALL_DIAGNOSTICS,
        )

    assert timeline == [
        "rpc.headers",
        "body-1",
        "body-2",
        "rpc.trailers",
        "rpc.completed",
    ]


def test_split_rpc_diagnostics_keep_one_ndjson_sequence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_rpc(
        monkeypatch,
        client_streaming=False,
        server_streaming=False,
        diagnostics=replace(ALL_DIAGNOSTICS, diagnostic_format="json"),
    )

    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [event["event"] for event in events] == [
        "rpc.headers",
        "rpc.trailers",
        "rpc.completed",
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_production_non_ok_summary_retains_numeric_status_context() -> None:
    sink = RecordingDiagnosticSink()
    summary = SimpleNamespace(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="INVALID_ARGUMENT", value=(3, "unused")),
        details="validation failed",
        sent_messages=1,
        received_messages=0,
        elapsed_seconds=0.001,
    )

    write_terminal_diagnostics(
        summary,
        ALL_DIAGNOSTICS,
        include_non_ok_status=True,
        sink=sink,
    )

    completed = sink.events[-1]
    assert completed.name == "rpc.completed"
    assert completed.fields["status"] == "INVALID_ARGUMENT"
    assert completed.fields["grpc_status"] == {
        "name": "INVALID_ARGUMENT",
        "number": 3,
    }
    assert completed.fields["sent"] == 1
    assert completed.fields["received"] == 0
    assert completed.fields["sent_messages"] == 1
    assert completed.fields["received_messages"] == 0


def test_production_metadata_phases_skip_only_empty_human_writes() -> None:
    diagnostics = DiagnosticOptions(True, True, False)
    summary = SimpleNamespace(
        initial_metadata=[],
        trailing_metadata=[("blank", " \t"), (" ", "visible")],
        code=OK,
        details="",
        sent_messages=1,
        received_messages=1,
        elapsed_seconds=0.001,
    )
    human_stream = io.StringIO()
    human_sink = StderrDiagnosticSink(0, stream=human_stream, color="never")
    json_stream = io.StringIO()
    json_sink = StderrDiagnosticSink(
        0, stream=json_stream, diagnostic_format="json"
    )

    for sink in (human_sink, json_sink):
        write_initial_diagnostics([], diagnostics, sink=sink)
        write_terminal_diagnostics(summary, diagnostics, sink=sink)

    assert human_stream.getvalue() == ""
    assert human_sink.sequence == 0
    events = [json.loads(line) for line in json_stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "rpc.headers",
        "rpc.trailers",
    ]
    assert [event["sequence"] for event in events] == [1, 2]
