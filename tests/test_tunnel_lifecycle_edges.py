import threading
from typing import Any, cast

import pytest

import protodial.transport_core.insecure as insecure_core
from protodial.transport_core.insecure import InsecureTlsTunnel


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False
        self.shutdown_calls = 0

    def shutdown(self, how: int) -> None:
        self.shutdown_calls += 1

    def close(self) -> None:
        self.closed = True


class FakeListener(FakeSocket):
    def setsockopt(self, *args: object) -> None:
        pass

    def bind(self, address: object) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def settimeout(self, timeout: float) -> None:
        pass


def test_start_thread_failure_closes_listener_without_masking_error(
    monkeypatch,
):
    class StartError(Exception):
        pass

    class FailingThread:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise StartError("accept thread failed")

        def join(self, timeout: float | None = None) -> None:
            raise AssertionError("an unstarted thread must not be joined")

    listener = FakeListener()
    tunnel = InsecureTlsTunnel("example.test:443")
    tunnel._use_unix_socket = False
    monkeypatch.setattr(tunnel, "_create_ssl_context", object)
    monkeypatch.setattr(insecure_core.socket, "socket", lambda *args: listener)
    monkeypatch.setattr(insecure_core.threading, "Thread", FailingThread)

    with pytest.raises(StartError, match="accept thread failed"):
        tunnel.start()

    tunnel.close()
    assert listener.closed
    assert tunnel._listener is None
    assert tunnel._accept_thread is None


def test_start_preserves_primary_when_directory_cleanup_fails(monkeypatch):
    class StartError(Exception):
        pass

    class FailingDirectory:
        name = "/private/protodial-test"

        def cleanup(self) -> None:
            raise OSError("directory cleanup")

    class FailingThread:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise StartError("accept thread failed")

    listener = FakeListener()
    monkeypatch.setattr(insecure_core.socket, "AF_UNIX", 1, raising=False)
    tunnel = InsecureTlsTunnel("example.test:443")
    monkeypatch.setattr(tunnel, "_create_ssl_context", object)
    monkeypatch.setattr(
        insecure_core, "_unix_socket_directory", FailingDirectory
    )
    monkeypatch.setattr(insecure_core.socket, "socket", lambda *args: listener)
    monkeypatch.setattr(insecure_core.os, "chmod", lambda *_args: None)
    monkeypatch.setattr(insecure_core.threading, "Thread", FailingThread)

    with pytest.raises(StartError, match="accept thread failed") as raised:
        tunnel.start()

    assert listener.closed
    assert getattr(raised.value, "_protodial_notes") == (
        "tunnel directory cleanup also failed: directory cleanup",
    )


def test_close_continues_after_directory_and_socket_cleanup_failures() -> None:
    events: list[str] = []

    class FailingDirectory:
        def cleanup(self) -> None:
            events.append("directory")
            raise OSError("directory cleanup")

    class FailingSocket(FakeSocket):
        def close(self) -> None:
            events.append("socket")
            raise OSError("socket cleanup")

    class FakeThread:
        def join(self, timeout: float | None = None) -> None:
            events.append("thread")

    tunnel = InsecureTlsTunnel("example.test:443")
    tunnel._listener_directory = cast(Any, FailingDirectory())
    tunnel._sockets.add(cast(Any, FailingSocket()))
    tunnel._accept_thread = cast(Any, FakeThread())
    tunnel._workers.add(cast(Any, FakeThread()))

    with pytest.raises(OSError, match="directory cleanup") as raised:
        tunnel.close()

    assert events == ["directory", "socket", "thread", "thread"]
    assert getattr(raised.value, "_protodial_notes") == (
        "tunnel socket cleanup also failed: socket cleanup",
    )


def test_worker_thread_failure_releases_client_and_connection_slot(monkeypatch):
    class StartError(Exception):
        pass

    class FailingThread:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise StartError("relay thread failed")

    client = FakeSocket()
    tunnel = InsecureTlsTunnel("example.test:443", max_connections=1)

    class Listener:
        calls = 0

        def accept(self):
            self.calls += 1
            if self.calls == 1:
                return client, ("127.0.0.1", 12345)
            tunnel._closed.set()
            raise OSError("listener stopped")

    tunnel._listener = cast(Any, Listener())
    monkeypatch.setattr(insecure_core.threading, "Thread", FailingThread)

    tunnel._accept_loop()

    assert client.closed
    assert tunnel._workers == set()
    assert tunnel._sockets == set()
    assert tunnel._slots.acquire(blocking=False)


def test_close_rejects_socket_returned_by_blocked_connect(monkeypatch):
    connect_entered = threading.Event()
    allow_connect = threading.Event()
    client = FakeSocket()
    remote = FakeSocket()

    def create_connection(*args: object, **kwargs: object) -> FakeSocket:
        connect_entered.set()
        assert allow_connect.wait(2)
        return remote

    class Context:
        def wrap_socket(self, *args: object, **kwargs: object) -> object:
            raise AssertionError(
                "TLS must not start after the tunnel is closed"
            )

    tunnel = InsecureTlsTunnel("example.test:443")
    tunnel._ssl_context = cast(Any, Context())
    tunnel._slots.acquire()
    worker = threading.Thread(target=tunnel._serve, args=(cast(Any, client),))
    with tunnel._lock:
        tunnel._workers.add(worker)
        tunnel._sockets.add(cast(Any, client))
    monkeypatch.setattr(
        insecure_core.socket, "create_connection", create_connection
    )
    worker.start()
    assert connect_entered.wait(2)

    closed = threading.Event()
    closer = threading.Thread(target=lambda: (tunnel.close(), closed.set()))
    closer.start()
    assert client.closed
    allow_connect.set()

    assert closed.wait(2)
    closer.join(2)
    worker.join(2)
    assert not closer.is_alive()
    assert not worker.is_alive()
    assert remote.closed
    assert remote not in tunnel._sockets


def test_close_rejects_tls_socket_wrapped_during_shutdown(monkeypatch):
    wrap_entered = threading.Event()
    allow_wrap = threading.Event()
    client = FakeSocket()
    raw_remote = FakeSocket()
    remote = FakeSocket()

    class Context:
        def wrap_socket(self, *args: object, **kwargs: object) -> FakeSocket:
            wrap_entered.set()
            assert allow_wrap.wait(2)
            return remote

    tunnel = InsecureTlsTunnel("example.test:443")
    tunnel._ssl_context = cast(Any, Context())
    tunnel._slots.acquire()
    worker = threading.Thread(target=tunnel._serve, args=(cast(Any, client),))
    with tunnel._lock:
        tunnel._workers.add(worker)
        tunnel._sockets.add(cast(Any, client))
    monkeypatch.setattr(
        insecure_core.socket,
        "create_connection",
        lambda *args, **kwargs: raw_remote,
    )
    worker.start()
    assert wrap_entered.wait(2)

    closed = threading.Event()
    closer = threading.Thread(target=lambda: (tunnel.close(), closed.set()))
    closer.start()
    assert raw_remote.closed
    allow_wrap.set()

    assert closed.wait(2)
    closer.join(2)
    worker.join(2)
    assert not closer.is_alive()
    assert not worker.is_alive()
    assert remote.closed
    assert remote not in tunnel._sockets
