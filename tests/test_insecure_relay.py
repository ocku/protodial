import socket
import ssl
import threading
from collections import deque

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

    def select(readers, writers, exceptional, timeout):
        selections.append((readers[:], writers[:], timeout))
        # Plaintext already buffered by SSL must be read without OS readiness.
        return (
            [s for s in readers if s is client or len(selections) % 2 == 0],
            writers,
            [],
        )

    monkeypatch.setattr(insecure.select, "select", select)
    tunnel._relay_bidirectionally(client, remote)
    assert tunnel.last_error is None
    assert b"".join(remote.writes) == b"abcd"
    assert b"".join(client.writes) == b"wxyz"
    assert remote.attempts[0] is remote.attempts[1]
    assert client.attempts[0] is client.attempts[1]
    assert client.blocking is remote.blocking is False
    assert client.shutdowns == remote.shutdowns == [socket.SHUT_WR]
    assert any(timeout == 0 for _, _, timeout in selections)


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


def test_relay_records_io_error(monkeypatch):
    tunnel = InsecureTlsTunnel("example.test:443")
    client, client_peer = socket.socketpair()
    remote, remote_peer = socket.socketpair()
    error = OSError("relay select failed")

    def fail(*args):
        raise error

    monkeypatch.setattr(insecure.select, "select", fail)
    try:
        tunnel._relay_bidirectionally(client, remote)
        assert tunnel.last_error is error
    finally:
        for connection in (client, client_peer, remote, remote_peer):
            connection.close()
