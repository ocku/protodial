import pytest

import protodial.transport as transport
from protodial.commands.models import ParsedTarget
from protodial.transport_core.insecure import (
    split_host_port,
)

from .support import FakeCompression, FakeGrpc, make_command


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
