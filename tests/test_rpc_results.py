import io
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import protodial.rpc as rpc_module
from protodial.commands.models import DiagnosticOptions, InvokeOptions
from protodial.errors import CliError
from protodial.presentation.diagnostics.context import use_sink
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    RecordingDiagnosticSink,
)
from protodial.presentation.diagnostics.rendering import StderrDiagnosticSink
from protodial.presentation.rpc import render_metadata
from protodial.rpc import RpcFailure, RpcSummary, invoke_rpc
from protodial.rpc_core.call import (
    write_initial_diagnostics,
    write_terminal_diagnostics,
)


class FakeStatus:
    def __init__(self, name: str) -> None:
        self.name = name


OK = FakeStatus("OK")
UNKNOWN = FakeStatus("UNKNOWN")
INTERNAL = FakeStatus("INTERNAL")
CANCELLED = FakeStatus("CANCELLED")


class FakeRpcError(Exception):
    def __init__(
        self, code: FakeStatus = INTERNAL, details: str = "failed"
    ) -> None:
        super().__init__(details)
        self._code = code
        self._details = details

    def code(self) -> FakeStatus:
        return self._code

    def details(self) -> str:
        return self._details


class FakeCall:
    def __init__(
        self,
        responses: list[Any],
        *,
        code: FakeStatus = OK,
        details: str = "",
        failure: BaseException | None = None,
    ) -> None:
        self.responses = responses
        self._code = code
        self._details = details
        self.failure = failure
        self.cancelled = False

    def __iter__(self):
        yield from self.responses
        if self.failure is not None:
            raise self.failure

    def initial_metadata(self) -> list[tuple[str, str | bytes]]:
        return [
            ("header", "one"),
            ("header", "two"),
            ("header-bin", b"\x00\xff"),
        ]

    def trailing_metadata(self) -> list[tuple[str, str | bytes]]:
        return [("trailer", "done"), ("trailer", "again")]

    def code(self) -> FakeStatus:
        return self._code

    def details(self) -> str:
        return self._details

    def result(self) -> Any:
        if self.failure is not None:
            raise self.failure
        return self.responses[0]

    def cancel(self) -> None:
        self.cancelled = True
        self._code = CANCELLED


class FakeRpc:
    def __init__(
        self, call: FakeCall, client_streaming: bool, server_streaming: bool
    ) -> None:
        self.call = call
        self.client_streaming = client_streaming
        self.server_streaming = server_streaming
        self.call_options: dict[str, Any] = {}

    def _consume_requests(self, request: Any) -> None:
        if self.client_streaming:
            list(request)

    def __call__(self, request: Any, **kwargs: Any) -> FakeCall:
        assert self.server_streaming
        self.call_options = kwargs
        self._consume_requests(request)
        return self.call

    def future(self, request: Any, **kwargs: Any) -> FakeCall:
        assert not self.server_streaming
        self.call_options = kwargs
        self._consume_requests(request)
        return self.call


FAKE_GRPC = SimpleNamespace(
    RpcError=FakeRpcError, StatusCode=SimpleNamespace(UNKNOWN=UNKNOWN)
)
NO_DIAGNOSTICS = DiagnosticOptions(False, False, False)
ALL_DIAGNOSTICS = DiagnosticOptions(True, True, True)
TEST_OPTIONS = InvokeOptions("{}", "json", "json", None, None, 1024)


def run_rpc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_streaming: bool,
    server_streaming: bool,
    call: FakeCall | None = None,
    diagnostics: DiagnosticOptions = NO_DIAGNOSTICS,
) -> tuple[RpcSummary, FakeCall, list[Any]]:
    responses = (
        ["response-1", "response-2"] if server_streaming else ["response"]
    )
    call = call or FakeCall(responses)
    written: list[Any] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    method = SimpleNamespace(
        client_streaming=client_streaming,
        server_streaming=server_streaming,
    )
    requests = iter(
        ["request-1", "request-2"] if client_streaming else ["request"]
    )

    summary = invoke_rpc(
        FakeRpc(call, client_streaming, server_streaming),
        requests,
        TEST_OPTIONS,
        1.0,
        method,
        [],
        None,
        None,
        FAKE_GRPC,
        diagnostics,
    )
    return summary, call, written


@pytest.mark.parametrize(
    ("client_streaming", "server_streaming", "sent", "received"),
    [
        (False, False, 1, 1),
        (False, True, 1, 2),
        (True, False, 2, 1),
        (True, True, 2, 2),
    ],
)
def test_all_cardinalities_return_complete_summaries(
    monkeypatch: pytest.MonkeyPatch,
    client_streaming: bool,
    server_streaming: bool,
    sent: int,
    received: int,
) -> None:
    summary, call, written = run_rpc(
        monkeypatch,
        client_streaming=client_streaming,
        server_streaming=server_streaming,
    )

    assert summary.initial_metadata == [
        ("header", "one"),
        ("header", "two"),
        ("header-bin", b"\x00\xff"),
    ]
    assert summary.trailing_metadata == [
        ("trailer", "done"),
        ("trailer", "again"),
    ]
    assert summary.code is OK
    assert summary.sent_messages == sent
    assert summary.received_messages == received
    assert summary.elapsed_seconds >= 0
    assert len(written) == received
    assert call.cancelled is False


@pytest.mark.parametrize("server_streaming", [False, True])
def test_wait_for_ready_reaches_each_rpc_call_shape(
    monkeypatch: pytest.MonkeyPatch, server_streaming: bool
) -> None:
    call = FakeCall(["response"])
    fake_rpc = FakeRpc(call, False, server_streaming)
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(
        client_streaming=False, server_streaming=server_streaming
    )

    invoke_rpc(
        fake_rpc,
        iter(["request"]),
        replace(TEST_OPTIONS, wait_for_ready=True),
        1.0,
        method,
        [],
        None,
        None,
        FAKE_GRPC,
        NO_DIAGNOSTICS,
    )

    assert fake_rpc.call_options["wait_for_ready"] is True


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


def test_partial_stream_failure_keeps_messages_and_reports_final_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = FakeRpcError()
    call = FakeCall(
        ["response-1", "response-2"],
        code=INTERNAL,
        details="stream failed",
        failure=error,
    )
    written: list[Any] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    method = SimpleNamespace(client_streaming=False, server_streaming=True)

    with pytest.raises(RpcFailure) as raised:
        invoke_rpc(
            FakeRpc(call, False, True),
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

    assert raised.value.error is error
    assert raised.value.summary.received_messages == 2
    assert raised.value.status_rendered is False
    assert written == ["response-1", "response-2"]
    diagnostics = capsys.readouterr()
    assert diagnostics.out == ""
    assert "header-bin: base64:AP8=" in diagnostics.err
    assert "< trailer: done\n< trailer: again\n<" in diagnostics.err
    assert "status:" not in diagnostics.err


def test_unary_input_failure_is_preserved_instead_of_reported_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = FakeCall(["response"])
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(client_streaming=False, server_streaming=False)

    def requests():
        raise CliError("invalid request input")
        yield "unreachable"

    with pytest.raises(CliError, match="invalid request input"):
        invoke_rpc(
            FakeRpc(call, False, False),
            requests(),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            NO_DIAGNOSTICS,
        )


def test_unary_rpc_rejects_zero_request_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = FakeCall(["response"])
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(client_streaming=False, server_streaming=False)

    with pytest.raises(CliError, match="request input is empty"):
        invoke_rpc(
            FakeRpc(call, False, False),
            iter(()),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            NO_DIAGNOSTICS,
        )


def test_unexpected_metadata_error_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenMetadataCall(FakeCall):
        def initial_metadata(self) -> list[tuple[str, str | bytes]]:
            raise RuntimeError("metadata implementation bug")

    call = BrokenMetadataCall(["response"])
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(client_streaming=False, server_streaming=False)

    with pytest.raises(RuntimeError, match="metadata implementation bug"):
        invoke_rpc(
            FakeRpc(call, False, False),
            iter(("request",)),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            NO_DIAGNOSTICS,
        )

    assert call.cancelled is True


def test_client_stream_input_failure_escapes_and_suppresses_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = FakeCall(["response"])
    written: list[Any] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    method = SimpleNamespace(client_streaming=True, server_streaming=False)

    def requests():
        yield "request"
        raise CliError("invalid request input")

    with pytest.raises(CliError, match="invalid request input"):
        invoke_rpc(
            FakeRpc(call, True, False),
            requests(),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            NO_DIAGNOSTICS,
        )

    assert written == []
    assert call.cancelled is False


def test_keyboard_interrupt_cancels_unary_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = FakeCall(["response"], failure=KeyboardInterrupt())
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(client_streaming=False, server_streaming=False)

    with pytest.raises(KeyboardInterrupt):
        invoke_rpc(
            FakeRpc(call, False, False),
            iter(["request"]),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            NO_DIAGNOSTICS,
        )

    assert call.cancelled is True


def test_keyboard_interrupt_writes_cancellation_summary_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    call = FakeCall(["response"], failure=KeyboardInterrupt())
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(client_streaming=False, server_streaming=False)

    with pytest.raises(KeyboardInterrupt):
        invoke_rpc(
            FakeRpc(call, False, False),
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

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "* CANCELLED in " in captured.err
    assert "status: CANCELLED" not in captured.err
    assert "sent 1 message" in captured.err
    assert "received 0 messages" in captured.err


def test_interrupted_status_uses_warning_color_and_is_emitted_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    call = FakeCall(["response"], failure=KeyboardInterrupt())
    monkeypatch.setattr(rpc_module, "write_response", lambda *_args: None)
    method = SimpleNamespace(client_streaming=False, server_streaming=False)
    diagnostics = replace(ALL_DIAGNOSTICS, color="always")

    with pytest.raises(KeyboardInterrupt):
        invoke_rpc(
            FakeRpc(call, False, False),
            iter(["request"]),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            diagnostics,
        )

    rendered = capsys.readouterr().err
    assert rendered.count("CANCELLED") == 1
    assert "\x1b[1;33m*\x1b[0m CANCELLED" in rendered
    assert "\x1b[1;32m" not in rendered


def test_broken_output_pipe_cancels_active_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = FakeCall(["response"])

    def fail_write(*_args: Any) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(rpc_module, "write_response", fail_write)
    method = SimpleNamespace(client_streaming=False, server_streaming=True)

    with pytest.raises(BrokenPipeError):
        invoke_rpc(
            FakeRpc(call, False, True),
            iter(["request"]),
            TEST_OPTIONS,
            1.0,
            method,
            [],
            None,
            None,
            FAKE_GRPC,
            NO_DIAGNOSTICS,
        )

    assert call.cancelled is True
