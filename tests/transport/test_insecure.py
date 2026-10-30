import os
import stat
from typing import Any, cast

import pytest

import protodial.transport as transport
import protodial.transport_core.insecure as insecure_core
from protodial.commands.models import ParsedTarget
from protodial.transport_core.insecure import (
    InsecureTlsTunnel,
    TunnelChannel,
)
from protodial.transport_core.tls import TlsMaterial

from .support import FakeCompression, FakeGrpc, make_command


def test_insecure_channel_uses_validated_decrypted_client_key(monkeypatch):
    command = make_command("insecure")
    command.transport.cert = "client.pem"
    command.transport.key = "encrypted-client-key.pem"
    command.transport.key_password = "secret"
    grpc = FakeGrpc()
    tunnels = []

    class FakeTunnel:
        target = "127.0.0.1:12345"
        last_error = None

        def __init__(self, target, port, **kwargs):
            self.remote_target = target
            self.remote_port = port
            self.kwargs = kwargs
            tunnels.append(self)

        def start(self):
            pass

        def close(self):
            pass

    material = TlsMaterial(None, b"validated-certificate", b"decrypted-key")
    monkeypatch.setattr(
        transport, "validate_tls_material", lambda command: material
    )
    monkeypatch.setattr(transport, "InsecureTlsTunnel", FakeTunnel)

    transport.create_channel(command, grpc)

    assert tunnels[0].kwargs["certificate_chain"] == b"validated-certificate"
    assert tunnels[0].kwargs["private_key"] == b"decrypted-key"
    assert "certfile" not in tunnels[0].kwargs
    assert "keyfile" not in tunnels[0].kwargs


@pytest.mark.parametrize("authority", [None, ""])
def test_insecure_channel_validates_authority_before_starting_tunnel(
    monkeypatch: pytest.MonkeyPatch, authority: str | None
) -> None:
    command = make_command("insecure")
    command.target = ParsedTarget(
        "opaque",
        authority=authority,
        tunnel_host="example.test",
        tunnel_port=443,
    )
    grpc = FakeGrpc()
    constructed = 0

    class FakeTunnel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal constructed
            constructed += 1

    monkeypatch.setattr(transport, "InsecureTlsTunnel", FakeTunnel)

    with pytest.raises(ValueError, match="no usable authority"):
        transport.create_channel(command, grpc)

    assert constructed == 0


def test_insecure_tunnel_loads_client_material_from_private_temporary_files(
    monkeypatch,
):
    loaded = {}

    class FakeContext:
        def set_alpn_protocols(self, protocols):
            self.protocols = protocols

        def load_cert_chain(self, certfile, keyfile):
            with open(certfile, "rb") as certificate_file:
                loaded["certificate"] = certificate_file.read()
            with open(keyfile, "rb") as key_file:
                loaded["key"] = key_file.read()
            loaded["modes"] = (
                stat.S_IMODE(os.stat(certfile).st_mode),
                stat.S_IMODE(os.stat(keyfile).st_mode),
            )
            loaded["paths"] = (certfile, keyfile)

    context = FakeContext()
    monkeypatch.setattr(
        insecure_core.ssl, "SSLContext", lambda protocol: context
    )
    tunnel = InsecureTlsTunnel(
        "example.test:443",
        certificate_chain=b"validated-certificate",
        private_key=b"decrypted-key",
    )

    assert tunnel._create_ssl_context() is context
    assert loaded["certificate"] == b"validated-certificate"
    assert loaded["key"] == b"decrypted-key"
    if os.name == "posix":
        assert loaded["modes"] == (0o600, 0o600)
    assert all(not os.path.exists(path) for path in loaded["paths"])


@pytest.mark.skipif(
    not hasattr(insecure_core.socket, "AF_UNIX"), reason="Unix-only security"
)
def test_insecure_tunnel_uses_a_private_unix_listener(monkeypatch):
    class Listener:
        def bind(self, path):
            self.path = path
            open(path, "wb").close()

        def listen(self, _backlog):
            pass

        def settimeout(self, _timeout):
            pass

        def close(self):
            pass

    class Thread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(
        insecure_core.socket, "socket", lambda *_args: Listener()
    )
    monkeypatch.setattr(insecure_core.threading, "Thread", Thread)
    tunnel = InsecureTlsTunnel("example.test:443")
    monkeypatch.setattr(tunnel, "_create_ssl_context", lambda: object())

    tunnel.start()
    try:
        assert tunnel.target.startswith("unix:")
        path = tunnel.target.removeprefix("unix:")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    finally:
        tunnel.close()

    assert not os.path.exists(path)


def test_insecure_tunnel_resets_remote_timeout_before_relay(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.timeouts = []
            self.closed = False

        def selected_alpn_protocol(self):
            return "h2"

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self, remote):
            self.remote = remote

        def wrap_socket(self, raw_remote, *, server_hostname):
            return self.remote

    client = FakeSocket()
    raw_remote = FakeSocket()
    remote = FakeSocket()
    tunnel = InsecureTlsTunnel("example.test:443", connect_timeout=0.25)
    tunnel._ssl_context = cast(Any, FakeContext(remote))
    tunnel._slots.acquire()
    relayed = []
    monkeypatch.setattr(
        insecure_core.socket,
        "create_connection",
        lambda *args, **kwargs: raw_remote,
    )
    monkeypatch.setattr(
        tunnel,
        "_relay_bidirectionally",
        lambda local, upstream: relayed.append(
            (local, upstream, tuple(upstream.timeouts))
        ),
    )

    tunnel._serve(cast(Any, client))

    assert relayed == [(client, remote, (None,))]


def test_insecure_channel_uses_loopback_and_preserves_authority(
    monkeypatch, capsys
):
    command = make_command("insecure")
    grpc = FakeGrpc()
    tunnels = []

    class FakeTunnel:
        target = "127.0.0.1:12345"
        last_error = None

        def __init__(self, target, port, **kwargs):
            self.remote_target = target
            self.remote_port = port
            self.kwargs = kwargs
            self.closed = False
            tunnels.append(self)

        def start(self):
            print(
                "warning: TLS server certificate verification is disabled",
                file=__import__("sys").stderr,
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(transport, "InsecureTlsTunnel", FakeTunnel)
    channel = transport.create_channel(command, grpc)

    assert isinstance(channel, TunnelChannel)
    assert grpc.calls == [
        (
            "insecure",
            "127.0.0.1:12345",
            {
                "options": [("grpc.default_authority", "example.test:443")],
                "compression": FakeCompression.NoCompression,
            },
        )
    ]
    assert "verification is disabled" in capsys.readouterr().err
    channel.close()
    assert grpc.channel.closed
    assert tunnels[0].closed
