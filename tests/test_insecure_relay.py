import os
import socket
import ssl
import threading
from collections import deque
from types import SimpleNamespace

import pytest

from protodial.transport_core import insecure
from protodial.transport_core.insecure import InsecureTlsTunnel


@pytest.mark.parametrize("want_read", [False, True])
def test_relay_serializes_ssl_retries_and_drains_pending(
    monkeypatch, want_read
):
    tunnel = InsecureTlsTunnel("example.test:443", buffer_size=4)
    owner = threading.get_ident()

    class Endpoint:
        def __init__(self, reads):
            self.reads = deque(reads)
            self.writes = []
            self.attempts = []
            self.blocking = None
            self.shutdowns = []

        def setblocking(self, value):
            self.blocking = value

        def pending(self):
            return bool(self.reads)

        def recv(self, size):
            assert threading.get_ident() == owner
            assert size == 4
            value = self.reads.popleft()
            if isinstance(value, Exception):
                raise value
            return value

        def send(self, data):
            assert threading.get_ident() == owner
            assert len(data) <= 4
            self.attempts.append(data)
            if len(self.attempts) == 1:
                if want_read:
                    raise ssl.SSLWantReadError()
                raise ssl.SSLWantWriteError()
            self.writes.append(data[:1])
            return 1

        def shutdown(self, how):
            self.shutdowns.append(how)

    client = Endpoint([b"abcd", b""])
    remote = Endpoint([ssl.SSLWantWriteError(), b"wxyz", b""])
    monkeypatch.setattr(insecure.ssl, "SSLSocket", Endpoint)
    selections = []

    class FakeSelector:
        def __init__(self):
            self.keys = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.keys.clear()

        def get_key(self, connection):
            return self.keys[connection]

        def register(self, connection, events):
            key = SimpleNamespace(fileobj=connection, events=events)
            self.keys[connection] = key
            return key

        def modify(self, connection, events):
            return self.register(connection, events)

        def unregister(self, connection):
            return self.keys.pop(connection)

        def select(self, timeout):
            selections.append((dict(self.keys), timeout))
            # Buffered SSL plaintext bypasses operating-system readiness.
            ready = {
                client,
                *([remote] if len(selections) % 2 == 0 else []),
            }
            return [
                (key, key.events)
                for connection, key in self.keys.items()
                if connection in ready
            ]

    monkeypatch.setattr(insecure.selectors, "DefaultSelector", FakeSelector)
    tunnel._relay_bidirectionally(client, remote)
    assert tunnel.last_error is None
    assert b"".join(remote.writes) == b"abcd"
    assert b"".join(client.writes) == b"wxyz"
    assert remote.attempts[0] is remote.attempts[1]
    assert client.attempts[0] is client.attempts[1]
    assert client.blocking is remote.blocking is False
    assert client.shutdowns == remote.shutdowns == [socket.SHUT_WR]
    assert any(timeout == 0 for _, timeout in selections)


def test_idle_relay_shutdown_is_bounded():
    tunnel = InsecureTlsTunnel("example.test:443")
    client, client_peer = socket.socketpair()
    remote, remote_peer = socket.socketpair()
    worker = threading.Thread(
        target=tunnel._relay_bidirectionally, args=(client, remote), daemon=True
    )
    try:
        worker.start()
        tunnel._closed.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert tunnel.last_error is None
    finally:
        for connection in (client, client_peer, remote, remote_peer):
            connection.close()
        worker.join(timeout=1)


def test_relay_handles_high_numbered_descriptors():
    if os.name == "nt":
        pytest.skip("high descriptor numbers are not supported on Windows")

    descriptors = []
    pairs = []
    try:
        try:
            while not descriptors or descriptors[-1] < 1024:
                descriptors.append(os.open(os.devnull, os.O_RDONLY))
        except OSError as error:
            pytest.skip(f"insufficient descriptor capacity: {error}")

        try:
            pairs = [socket.socketpair(), socket.socketpair()]
        except OSError as error:
            pytest.skip(
                f"insufficient descriptor capacity for sockets: {error}"
            )
        client, client_peer = pairs[0]
        remote, remote_peer = pairs[1]
        if min(client.fileno(), remote.fileno()) < 1024:
            pytest.skip("could not allocate high-numbered relay sockets")

        client_peer.sendall(b"client")
        remote_peer.sendall(b"remote")
        client_peer.shutdown(socket.SHUT_WR)
        remote_peer.shutdown(socket.SHUT_WR)

        tunnel = InsecureTlsTunnel("example.test:443")
        tunnel._relay_bidirectionally(client, remote)

        assert tunnel.last_error is None
        assert client_peer.recv(16) == b"remote"
        assert remote_peer.recv(16) == b"client"
    finally:
        for pair in pairs:
            for connection in pair:
                connection.close()
        for descriptor in descriptors:
            os.close(descriptor)


def test_relay_records_io_error(monkeypatch):
    tunnel = InsecureTlsTunnel("example.test:443")
    client, client_peer = socket.socketpair()
    remote, remote_peer = socket.socketpair()
    error = OSError("relay selector failed")

    class FailingSelector:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_key(self, connection):
            raise KeyError(connection)

        def register(self, *args):
            pass

        def select(self, timeout):
            raise error

    monkeypatch.setattr(insecure.selectors, "DefaultSelector", FailingSelector)
    try:
        tunnel._relay_bidirectionally(client, remote)
        assert tunnel.last_error is error
    finally:
        for connection in (client, client_peer, remote, remote_peer):
            connection.close()
