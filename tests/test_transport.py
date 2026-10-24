import os
import stat
from types import SimpleNamespace
from typing import Any, cast

import pytest

import protodial.transport as transport
import protodial.transport_core.insecure as insecure_core
from protodial.commands.models import ParsedTarget
from protodial.transport_core.insecure import (
    InsecureTlsTunnel,
    TunnelChannel,
    split_host_port,
)
from protodial.transport_core.options import (
    build_channel_options,
    map_compression,
)
from protodial.transport_core.tls import TlsMaterial


class FakeCompression:
    NoCompression = object()
    Gzip = object()
    Deflate = object()


class FakeChannel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeGrpc:
    Compression = FakeCompression

    class FutureTimeoutError(Exception):
        pass

    def __init__(self) -> None:
        self.calls = []
        self.channel = FakeChannel()
        self.future = SimpleNamespace(
            result=lambda timeout: None, cancel=lambda: None
        )

    def insecure_channel(self, target, **kwargs):
        self.calls.append(("insecure", target, kwargs))
        return self.channel

    def secure_channel(self, target, credentials, **kwargs):
        self.calls.append(("secure", target, credentials, kwargs))
        return self.channel

    def ssl_channel_credentials(self, **kwargs):
        self.calls.append(("credentials", kwargs))
        return "credentials"

    def channel_ready_future(self, channel):
        assert channel is self.channel
        return self.future


def make_command(mode="verified", **connection_overrides):
    connection = {
        "connect_timeout": 2.5,
        "max_recv_message_size": None,
        "max_send_message_size": None,
        "compression": "none",
        "keepalive_time": None,
        "user_agent": None,
    }
    connection.update(connection_overrides)
    return SimpleNamespace(
        target=ParsedTarget(
            "example.test:443",
            authority="example.test:443",
            tunnel_host="example.test",
            tunnel_port=443,
        ),
        transport=SimpleNamespace(
            mode=mode,
            ca_paths=(),
            cert=None,
            key=None,
            key_password=None,
            server_name=None,
            authority=None,
        ),
        connection=SimpleNamespace(**connection),
    )


def test_channel_options_include_limits_identity_and_connection_controls():
    command = make_command(
        max_recv_message_size=1024,
        max_send_message_size=-1,
        keepalive_time=1.25,
        user_agent="protodial-test/1",
    )
    command.transport.server_name = "certificate.test"
    command.transport.authority = "route.test"

    assert build_channel_options(command) == [
        ("grpc.ssl_target_name_override", "certificate.test"),
        ("grpc.default_authority", "route.test"),
        ("grpc.max_receive_message_length", 1024),
        ("grpc.max_send_message_length", -1),
        ("grpc.keepalive_time_ms", 1250),
        ("grpc.primary_user_agent", "protodial-test/1"),
    ]


@pytest.mark.parametrize("value", [0, -2, True, 1.5])
def test_message_limit_validation(value):
    command = make_command(max_recv_message_size=value)
    with pytest.raises(ValueError, match="positive or -1"):
        build_channel_options(command)


def test_compression_mapping_uses_public_enum():
    grpc = FakeGrpc()
    assert map_compression(grpc, "none") is FakeCompression.NoCompression
    assert map_compression(grpc, "GZIP") is FakeCompression.Gzip
    assert map_compression(grpc, "deflate") is FakeCompression.Deflate
    with pytest.raises(ValueError, match="unsupported compression"):
        map_compression(grpc, "brotli")


def test_tls_material_rejects_empty_and_malformed_files(tmp_path):
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    command = make_command()
    command.transport.ca_paths = (str(empty),)
    with pytest.raises(ValueError, match="empty"):
        transport.validate_tls_material(command)

    malformed = tmp_path / "malformed.pem"
    malformed.write_text("not a certificate")
    command.transport.ca_paths = (str(malformed),)
    with pytest.raises(ValueError, match="PEM certificate"):
        transport.validate_tls_material(command)


def test_tls_material_decrypts_password_protected_key_for_grpc(
    tmp_path, monkeypatch
):
    cert = tmp_path / "client.pem"
    key = tmp_path / "client-key.pem"
    cert.write_bytes(b"certificate")
    key.write_bytes(b"encrypted-key")
    command = make_command()
    command.transport.cert = str(cert)
    command.transport.key = str(key)
    command.transport.key_password = "secret"
    calls = []

    class FakeContext:
        def __init__(self, protocol):
            calls.append(("context", protocol))

        def load_cert_chain(self, **kwargs):
            calls.append(("load_cert_chain", kwargs))

    class FakeKey:
        def private_bytes(self, encoding, private_format, encryption):
            calls.append(
                ("private_bytes", encoding, private_format, encryption)
            )
            return b"decrypted-key"

    def load_private_key(data, password):
        calls.append(("load_private_key", data, password))
        return FakeKey()

    monkeypatch.setattr(transport.ssl, "SSLContext", FakeContext)
    monkeypatch.setattr(transport, "load_pem_private_key", load_private_key)

    material = transport.validate_tls_material(command)

    assert material.private_key == b"decrypted-key"
    assert calls[1] == (
        "load_cert_chain",
        {"certfile": str(cert), "keyfile": str(key), "password": "secret"},
    )
    assert calls[2] == ("load_private_key", b"encrypted-key", b"secret")


def test_tls_material_rejects_unsafe_mode_combinations():
    command = make_command("plaintext")
    command.transport.server_name = "example.test"
    with pytest.raises(ValueError, match="plaintext"):
        transport.validate_tls_material(command)

    command = make_command("insecure")
    command.transport.ca_paths = ("unused.pem",)
    with pytest.raises(ValueError, match="cannot be used"):
        transport.validate_tls_material(command)


def test_verified_channel_passes_credentials_options_and_compression():
    command = make_command(max_recv_message_size=4096, compression="gzip")
    grpc = FakeGrpc()

    channel = transport.create_channel(command, grpc)

    assert channel is grpc.channel
    assert grpc.calls[0] == (
        "credentials",
        {
            "root_certificates": None,
            "private_key": None,
            "certificate_chain": None,
        },
    )
    assert grpc.calls[1] == (
        "secure",
        "example.test:443",
        "credentials",
        {
            "options": [("grpc.max_receive_message_length", 4096)],
            "compression": FakeCompression.Gzip,
        },
    )


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


def test_plaintext_channel_preserves_resolver_authority_target():
    command = make_command("plaintext")
    command.target = ParsedTarget(
        "dns://resolver.example/service.example:443",
        authority="service.example:443",
    )
    grpc = FakeGrpc()

    transport.create_channel(command, grpc)

    assert grpc.calls == [
        (
            "insecure",
            "dns://resolver.example/service.example:443",
            {
                "options": [],
                "compression": FakeCompression.NoCompression,
            },
        )
    ]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("localhost:443", ("localhost", 443)),
        ("127.0.0.1:8443", ("127.0.0.1", 8443)),
        ("[::1]:443", ("::1", 443)),
    ],
)
def test_split_host_port(target, expected):
    assert split_host_port(target) == expected


@pytest.mark.parametrize(
    "target", ["dns:///example:443", "::1:443", "host", "host:0"]
)
def test_split_host_port_rejects_non_authorities(target):
    with pytest.raises(ValueError):
        split_host_port(target)
