from types import SimpleNamespace
from typing import Any

import pytest

import protodial.rpc as rpc_module
from protodial.errors import CliError
from protodial.rpc import RpcFailure, invoke_rpc

from .support import (
    ALL_DIAGNOSTICS,
    FAKE_GRPC,
    INTERNAL,
    NO_DIAGNOSTICS,
    TEST_OPTIONS,
    FakeCall,
    FakeRpc,
    FakeRpcError,
)


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
