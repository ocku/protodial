from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import protodial.rpc as rpc_module
from protodial.rpc import invoke_rpc

from .support import (
    ALL_DIAGNOSTICS,
    FAKE_GRPC,
    NO_DIAGNOSTICS,
    TEST_OPTIONS,
    FakeCall,
    FakeRpc,
    run_rpc,
)


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


@pytest.mark.parametrize("client_streaming", [False, True])
@pytest.mark.parametrize("server_streaming", [False, True])
def test_interrupt_survives_failed_cancellation_at_invocation_boundary(
    monkeypatch: pytest.MonkeyPatch,
    client_streaming: bool,
    server_streaming: bool,
) -> None:
    interruption = KeyboardInterrupt()

    class CancelFailure(FakeCall):
        attempts = 0

        def cancel(self) -> None:
            self.attempts += 1
            raise OSError("cancel failed")

        def trailing_metadata(self) -> list[tuple[str, str | bytes]]:
            pytest.fail("cannot collect terminal metadata after cancel failed")

    call = CancelFailure([], failure=interruption)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_rpc(
            monkeypatch,
            client_streaming=client_streaming,
            server_streaming=server_streaming,
            call=call,
            diagnostics=ALL_DIAGNOSTICS,
        )

    assert caught.value is interruption
    assert call.attempts == 1
    assert getattr(interruption, "_protodial_notes", ()) == (
        "RPC cancellation also failed: cancel failed",
    )


@pytest.mark.parametrize("client_streaming", [False, True])
@pytest.mark.parametrize("server_streaming", [False, True])
def test_interrupt_survives_failed_terminal_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    client_streaming: bool,
    server_streaming: bool,
) -> None:
    interruption = KeyboardInterrupt()

    class SummaryFailure(FakeCall):
        def trailing_metadata(self) -> list[tuple[str, str | bytes]]:
            raise OSError("summary failed")

    call = SummaryFailure([], failure=interruption)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_rpc(
            monkeypatch,
            client_streaming=client_streaming,
            server_streaming=server_streaming,
            call=call,
            diagnostics=ALL_DIAGNOSTICS,
        )

    assert caught.value is interruption
    assert call.cancelled
    assert getattr(interruption, "_protodial_notes", ()) == (
        "Interrupted RPC diagnostics also failed: summary failed",
    )
