import pytest

import protodial.schema.context as schema_context
from protodial.argument_parser import parse_arguments, parse_target
from protodial.commands.operations import Invoke
from protodial.errors import CliError


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("localhost:50051", ("localhost:50051", None, None)),
        ("grpc://localhost:50051", ("localhost:50051", None, "plaintext")),
        ("grpcs://localhost:50051", ("localhost:50051", None, "tls")),
        ("[::1]:50051", ("[::1]:50051", None, None)),
        ("::1:50051", ("::1:50051", None, None)),
        ("dns:///localhost:50051", ("dns:///localhost:50051", None, None)),
        ("unix:///tmp/example.sock", ("unix:///tmp/example.sock", None, None)),
        (
            "grpc://localhost:50051/example.Greeter/SayHello",
            ("localhost:50051", "example.Greeter/SayHello", "plaintext"),
        ),
        (
            "grpcs://[::1]:50051/example.Greeter/SayHello",
            ("[::1]:50051", "example.Greeter/SayHello", "tls"),
        ),
    ],
)
def test_parse_target_forms(
    target: str, expected: tuple[str, str | None, str | None]
) -> None:
    parsed = parse_target(target)
    assert (parsed.channel, parsed.method, parsed.transport) == expected


@pytest.mark.parametrize(
    ("target", "authority"),
    [
        ("unix:/tmp/example.sock", None),
        ("unix-abstract:protodial-test", None),
        ("ipv4:127.0.0.1:50051", "127.0.0.1:50051"),
        (
            "ipv4:127.0.0.1:50051,127.0.0.1:50052",
            None,
        ),
        ("dns://resolver.example/service.example:443", "service.example:443"),
        ("custom-resolver:opaque/value", None),
    ],
)
def test_parse_native_grpcio_targets(
    target: str, authority: str | None
) -> None:
    parsed = parse_target(target)

    assert parsed.channel == target
    assert parsed.authority == authority


def test_parse_tcp_authority_retains_tunnel_endpoint() -> None:
    parsed = parse_target("localhost:50051")

    assert parsed.authority == "localhost:50051"
    assert parsed.tunnel_host == "localhost"
    assert parsed.tunnel_port == 50051


@pytest.mark.parametrize(
    "target",
    [
        "dns:///",
        "user@localhost:50051",
        "[::1]garbage:50051",
    ],
)
def test_parse_target_rejects_malformed_uris_and_authorities(
    target: str,
) -> None:
    with pytest.raises(CliError):
        parse_target(target)


def test_parse_url_style_invocation() -> None:
    command = parse_arguments(
        [
            "-d",
            '{"name":"Ada"}',
            "grpc://localhost:50051/example.Greeter/SayHello",
        ]
    )

    assert command.target.channel == "localhost:50051"
    assert command.operation == Invoke("example.Greeter/SayHello")
    assert command.transport.plaintext is True
    assert command.invoke_options is not None
    assert command.invoke_options.data == '{"name":"Ada"}'


@pytest.mark.parametrize(
    "target",
    [
        "dns:///localhost:50051",
        "unix:///tmp/example.sock",
        "unix:/tmp/example.sock",
        "unix-abstract:protodial-test",
        "ipv4:127.0.0.1:50051",
        "dns://resolver.example/service.example:50051",
    ],
)
def test_parse_opaque_target_with_explicit_method(target: str) -> None:
    command = parse_arguments(
        [
            "--plaintext",
            "--method",
            "example.Greeter/SayHello",
            target,
        ]
    )

    assert command.target.channel == target
    assert command.operation == Invoke("example.Greeter/SayHello")


def test_method_operand_is_rejected_when_url_already_contains_method() -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(
            [
                "grpc://localhost:50051/example.Greeter/SayHello",
                "example.Greeter/Other",
            ]
        )

    assert error.value.code == 2


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            [
                "--plaintext",
                "--method",
                "pkg.API/Call",
                "dns:///service.test:50051",
            ],
            "service.test:50051",
        ),
        (
            [
                "--plaintext",
                "--method",
                "pkg.API/Call",
                "unix:///tmp/service.sock",
            ],
            "",
        ),
        (
            [
                "--plaintext",
                "--authority",
                "virtual.test",
                "--method",
                "pkg.API/Call",
                "dns:///service.test:50051",
            ],
            "virtual.test",
        ),
    ],
)
def test_reflection_host_uses_effective_authority(
    arguments: list[str], expected: str
) -> None:
    assert (
        schema_context._reflection_host(parse_arguments(arguments)) == expected
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--insecure", "grpcs://example.test/example.Echo/Call"],
        ["list", "--insecure", "dns:///example.test:443"],
        ["list", "--insecure", "::1:443"],
    ],
)
def test_insecure_tls_rejects_targets_the_bridge_cannot_dial(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(arguments)

    assert error.value.code == 2
