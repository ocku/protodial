from types import SimpleNamespace
from typing import Any, cast

import pytest

import protodial.transport as transport
from protodial.transport_core.insecure import (
    TunnelChannel,
)

from .support import FakeGrpc, make_command


def test_ready_channel_waits_and_always_closes():
    command = make_command("plaintext")
    grpc = FakeGrpc()
    seen = []
    grpc.future = SimpleNamespace(
        result=lambda timeout: seen.append(("result", timeout)),
        cancel=lambda: seen.append(("cancel", None)),
    )

    with transport.ready_channel(command, grpc) as channel:
        assert channel is grpc.channel
        assert not channel.closed

    assert seen == [("result", 2.5), ("cancel", None)]
    assert grpc.channel.closed


def test_ready_channel_can_defer_readiness_to_the_rpc():
    command = make_command("plaintext", connect_timeout=0.01)
    grpc = FakeGrpc()

    def unexpected_future(_channel):
        raise AssertionError("readiness preflight must be skipped")

    grpc.channel_ready_future = unexpected_future

    with transport.ready_channel(command, grpc, await_ready=False) as channel:
        assert channel is grpc.channel
        assert not channel.closed

    assert grpc.channel.closed


def test_ready_channel_closes_channel_and_tunnel_if_future_construction_fails(
    monkeypatch,
):
    command = make_command("insecure")
    grpc = FakeGrpc()

    class FakeTunnel:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    tunnel = FakeTunnel()
    channel = TunnelChannel(grpc.channel, cast(Any, tunnel))
    monkeypatch.setattr(
        transport, "create_channel", lambda command, grpc: channel
    )

    def fail_to_create_future(_channel):
        raise RuntimeError("future construction failed")

    monkeypatch.setattr(grpc, "channel_ready_future", fail_to_create_future)

    with pytest.raises(RuntimeError, match="future construction failed"):
        with transport.ready_channel(command, grpc):
            pytest.fail("channel must not be yielded")

    assert grpc.channel.closed
    assert tunnel.closed


def test_ready_channel_cancels_and_closes_on_timeout():
    command = make_command("plaintext", connect_timeout=0.25)
    grpc = FakeGrpc()
    cancelled = []

    def timeout(_timeout):
        raise grpc.FutureTimeoutError()

    grpc.future = SimpleNamespace(
        result=timeout, cancel=lambda: cancelled.append(True)
    )
    with pytest.raises(TimeoutError, match="0.25"):
        with transport.ready_channel(command, grpc):
            pytest.fail("channel must not be yielded")

    assert cancelled
    assert grpc.channel.closed


def test_ready_channel_preserves_primary_when_channel_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = make_command("plaintext")
    grpc = FakeGrpc()
    primary = RuntimeError("primary RPC failure")

    def fail_close() -> None:
        raise OSError("channel close failed")

    grpc.channel.close = fail_close
    monkeypatch.setattr(
        transport, "create_channel", lambda _command, _grpc: grpc.channel
    )

    with pytest.raises(RuntimeError, match="primary RPC failure") as raised:
        with transport.ready_channel(command, grpc):
            raise primary

    assert raised.value is primary
    assert getattr(primary, "_protodial_notes") == (
        "channel cleanup also failed: channel close failed",
    )


def test_ready_channel_preserves_primary_when_cancellation_fails() -> None:
    command = make_command("plaintext")
    grpc = FakeGrpc()
    primary = ValueError("readiness primary")

    def fail_result(_timeout: float) -> None:
        raise primary

    def fail_cancel() -> None:
        raise OSError("cancel cleanup")

    grpc.future = SimpleNamespace(result=fail_result, cancel=fail_cancel)

    with pytest.raises(ValueError, match="readiness primary") as raised:
        with transport.ready_channel(command, grpc):
            pytest.fail("channel must not be yielded")

    assert raised.value is primary
    assert grpc.channel.closed
    assert getattr(primary, "_protodial_notes") == (
        "readiness cancellation also failed: cancel cleanup",
    )


def test_ready_channel_continues_cleanup_after_cancellation_fails() -> None:
    command = make_command("plaintext")
    grpc = FakeGrpc()

    def fail_cancel() -> None:
        raise OSError("cancel cleanup")

    grpc.future = SimpleNamespace(
        result=lambda _timeout: None, cancel=fail_cancel
    )

    with pytest.raises(OSError, match="cancel cleanup"):
        with transport.ready_channel(command, grpc):
            pass

    assert grpc.channel.closed


def test_ready_channel_retains_nested_channel_cleanup_notes() -> None:
    command = make_command("plaintext")
    grpc = FakeGrpc()

    class FailingChannel:
        def close(self) -> None:
            raise OSError("channel cleanup")

    class FailingTunnel:
        def close(self) -> None:
            error = OSError("tunnel cleanup")
            setattr(error, "_protodial_notes", ("worker cleanup also failed",))
            raise error

    channel = TunnelChannel(cast(Any, FailingChannel()), FailingTunnel())
    grpc.channel = cast(Any, channel)

    def fail_cancel() -> None:
        raise OSError("cancel cleanup")

    grpc.future = SimpleNamespace(
        result=lambda _timeout: None, cancel=fail_cancel
    )

    with pytest.raises(OSError, match="cancel cleanup") as raised:
        with transport.ready_channel(command, grpc):
            pass

    assert getattr(raised.value, "_protodial_notes") == (
        "channel cleanup also failed: channel cleanup",
        "channel cleanup also failed detail: "
        "tunnel cleanup also failed: tunnel cleanup",
        "channel cleanup also failed detail: tunnel cleanup also failed "
        "detail: worker cleanup also failed",
    )


def test_tunnel_channel_preserves_channel_close_failure() -> None:
    class FailingChannel:
        def close(self) -> None:
            raise OSError("channel close failed")

    class FailingTunnel:
        def close(self) -> None:
            error = OSError("tunnel close failed")
            setattr(error, "_protodial_notes", ("socket cleanup failed",))
            raise error

    channel = TunnelChannel(cast(Any, FailingChannel()), FailingTunnel())

    with pytest.raises(OSError, match="channel close failed") as raised:
        channel.close()

    assert getattr(raised.value, "_protodial_notes") == (
        "tunnel cleanup also failed: tunnel close failed",
        "tunnel cleanup also failed detail: socket cleanup failed",
    )
