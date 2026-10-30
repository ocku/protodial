from dataclasses import replace
from types import SimpleNamespace

import pytest

import protodial.rpc as rpc_module
from protodial.rpc import invoke_rpc

from .support import (
    FAKE_GRPC,
    NO_DIAGNOSTICS,
    OK,
    TEST_OPTIONS,
    FakeCall,
    FakeRpc,
    run_rpc,
)


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
