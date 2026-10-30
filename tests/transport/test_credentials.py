import pytest

import protodial.transport as transport
from protodial.transport_core.options import (
    build_channel_options,
    map_compression,
)

from .support import FakeCompression, FakeGrpc, make_command


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
