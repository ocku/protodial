import contextlib
import errno
import io
import json
import struct
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import protodial.cli as cli
import protodial.schema.context as schema_context
import protodial.transport as transport
from protodial.argument_parser import parse_arguments, parse_target
from protodial.commands.models import Command
from protodial.commands.operations import Describe, Invoke, ListSymbols
from protodial.errors import CliError
from protodial.formats import MessageFormat
from protodial.message_io import iter_requests
from protodial.rpc import (
    RpcFailure,
    RpcSummary,
    open_input,
    parse_metadata,
    read_exact,
)
from protodial.schema.context import _export_after_use
from protodial.schema.paths import (
    default_proto_paths,
    ensure_default_proto_paths,
)


@pytest.fixture(autouse=True)
def _isolate_default_proto_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


class FakeRpcError(Exception):
    def code(self) -> object:
        return SimpleNamespace(name="INTERNAL")

    def details(self) -> str:
        return str(self)


def test_parse_invocation_uses_documented_defaults() -> None:
    command = parse_arguments(["grpcs://localhost:50051/example.Echo/Call"])

    assert command.target.channel == "localhost:50051"
    assert command.operation == Invoke("example.Echo/Call")
    assert command.invoke_options is not None
    assert command.invoke_options.data is None
    assert command.invoke_options.input_format is MessageFormat.JSON
    assert command.invoke_options.output_format is MessageFormat.JSON
    assert command.invoke_options.chunk_size == 65_536
    assert command.schema.proto_path == default_proto_paths()
    assert command.timeout == 30
    assert command.diagnostics.show_headers is False
    assert command.diagnostics.show_trailers is False
    assert command.diagnostics.show_status is False


def test_explicit_proto_paths_extend_the_default() -> None:
    command = parse_arguments(
        ["--proto-path", "protos", "grpcs://localhost:50051/example.Echo/Call"]
    )

    assert command.schema.proto_path == (*default_proto_paths(), "protos")


def test_default_proto_paths_follow_xdg_and_create_only_when_requested(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    xdg_data_home = tmp_path / "xdg-data-home"
    paths = default_proto_paths(
        {"HOME": str(home), "XDG_DATA_HOME": str(xdg_data_home)}
    )

    assert paths == (str(xdg_data_home / "protodial" / "protos"),)
    assert not Path(paths[0]).exists()
    assert ensure_default_proto_paths(paths) == paths
    assert all(Path(path).is_dir() for path in paths)


def test_default_proto_paths_fall_back_to_home_data_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    assert default_proto_paths({"HOME": str(home)}) == (
        str(home / ".local" / "share" / "protodial" / "protos"),
    )


@pytest.mark.parametrize("use_xdg", [False, True])
@pytest.mark.parametrize(
    ("arguments", "handler"),
    [
        (["grpc://localhost:50051/example.Echo/Call"], "invoke"),
        (["list", "localhost:50051"], "list_symbols"),
        (["describe", "localhost:50051", "example.Echo"], "describe"),
    ],
)
def test_commands_create_only_default_proto_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    use_xdg: bool,
    arguments: list[str],
    handler: str,
) -> None:
    if use_xdg:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    defaults = default_proto_paths()
    custom = tmp_path / "custom-import-root"
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    calls = []

    def run(command: Command, _grpc: object) -> None:
        assert all(Path(path).is_dir() for path in defaults)
        assert str(custom) in command.schema.proto_path
        calls.append(command)

    monkeypatch.setattr(cli, handler, run)
    # Discovery options follow the subcommand; invocation options precede
    # the target. Repeating the command must preserve existing directories.
    position = 0 if handler == "invoke" else 1
    options = ["--proto-path", str(custom)]
    argv = [*arguments[:position], *options, *arguments[position:]]
    for _ in range(2):
        assert cli.main(argv) == 0
    assert len(calls) == 2
    assert not custom.exists()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("arguments", [["--help"], ["--version"], []])
def test_nonexecuting_commands_do_not_create_proto_directories(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == (2 if not arguments else 0)
    assert all(not Path(path).exists() for path in default_proto_paths())


def test_default_proto_directory_creation_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file cannot contain proto directories")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocked))

    def unexpected_runtime() -> object:
        pytest.fail("directory creation must finish before loading the runtime")

    monkeypatch.setattr(cli, "load_runtime", unexpected_runtime)
    assert (
        cli.main(["list", "--diagnostic-format", "json", "localhost:50051"])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    diagnostic = json.loads(captured.err)
    assert "cannot create default proto directory" in diagnostic["message"]
    assert str(blocked) in diagnostic["message"]


@pytest.mark.parametrize("input_format", ["json", "text", "protobuf", "raw"])
def test_parse_invocation_preserves_omitted_data(input_format: str) -> None:
    command = parse_arguments(
        [
            "--input-format",
            input_format,
            "grpcs://localhost:50051/example.Echo/Call",
        ]
    )

    assert command.invoke_options is not None
    assert command.invoke_options.input_format is MessageFormat(input_format)
    assert command.invoke_options.data is None


def test_field_file_selects_raw_input_and_static_fields() -> None:
    command = parse_arguments(
        [
            "--field",
            "filename=video.bin",
            "--field",
            "part=2",
            "--field:file",
            "content=video.bin",
            "grpcs://localhost:50051/example.Upload/Send",
        ]
    )

    assert command.invoke_options is not None
    assert command.invoke_options.input_format is MessageFormat.RAW
    assert command.invoke_options.input_field == "content"
    assert command.invoke_options.data == "@video.bin"
    assert command.invoke_options.fields == (
        ("filename", "video.bin"),
        ("part", "2"),
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--field:file", "content="],
        ["--field:file", "content=a", "--field:file", "content=b"],
        ["--field:file", "content=a", "--data", "@b"],
        ["--field:file", "content=a", "--input-field", "content"],
        ["--field:file", "content=a", "--input-format", "json"],
        ["--field", "filename=video.bin"],
        ["--input-format", "raw", "--field", "missing=value"],
    ],
)
def test_invalid_field_input_combinations_are_rejected(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(
            [*options, "grpcs://localhost:50051/example.Upload/Send"]
        )

    assert error.value.code == 2
    assert capsys.readouterr().err


def test_interactive_is_rejected_instead_of_silently_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(
            [
                "--interactive",
                "--data",
                "@-",
                "grpcs://localhost:50051/example.Echo/Call",
            ]
        )

    assert error.value.code == 2
    assert "--interactive is not supported" in capsys.readouterr().err


def test_parse_client_key_password() -> None:
    command = parse_arguments(
        [
            "--cert",
            "client.pem",
            "--key",
            "client-key.pem",
            "--key-password",
            "secret",
            "grpcs://localhost:50051/example.Echo/Call",
        ]
    )

    assert command.transport.key_password == "secret"


def test_key_password_requires_client_key() -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(
            [
                "--key-password",
                "secret",
                "grpcs://localhost:50051/example.Echo/Call",
            ]
        )

    assert error.value.code == 2


def test_verbose_and_focused_response_diagnostics() -> None:
    verbose = parse_arguments(
        ["--verbose", "grpcs://localhost:50051/example.Echo/Call"]
    )
    trailers = parse_arguments(
        ["--show-trailers", "grpcs://localhost:50051/example.Echo/Call"]
    )

    assert verbose.diagnostics.show_headers is True
    assert verbose.diagnostics.show_trailers is True
    assert verbose.diagnostics.show_status is True
    assert trailers.diagnostics.show_headers is False
    assert trailers.diagnostics.show_trailers is True
    assert trailers.diagnostics.show_status is False


@pytest.mark.parametrize(
    ("argv", "operation"),
    [
        (["list", "localhost:50051"], ListSymbols(None)),
        (
            ["list", "grpc://localhost:50051", "example.Echo"],
            ListSymbols("example.Echo"),
        ),
        (
            ["describe", "grpcs://localhost:50051", "example.Echo"],
            Describe("example.Echo"),
        ),
        (
            ["grpc://localhost:50051/example.Echo/list"],
            Invoke("example.Echo/list"),
        ),
        (
            ["grpcs://localhost:50051/example.Echo/describe"],
            Invoke("example.Echo/describe"),
        ),
        (
            ["describe", "grpcs://localhost:50051/example.Echo"],
            Describe("example.Echo"),
        ),
        (
            [
                "describe",
                "grpcs://localhost:50051/example.Echo/Call",
            ],
            Describe("example.Echo/Call"),
        ),
        (
            ["list", "grpc://localhost:50051/example.Echo"],
            ListSymbols("example.Echo"),
        ),
    ],
)
def test_parse_operation_forms(argv: list[str], operation: object) -> None:
    command = parse_arguments(argv)

    assert isinstance(command, Command)
    assert command.operation == operation


def test_describe_template_is_a_subcommand_option_and_accepts_url_symbol() -> (
    None
):
    command = parse_arguments(
        [
            "describe",
            "--template",
            "--template-depth",
            "4",
            "grpc://localhost:50054/examples.formats.Document",
        ]
    )

    assert command.operation == Describe("examples.formats.Document")
    assert command.get_template is True
    assert command.template_depth == 4

    method = parse_arguments(
        [
            "describe",
            "--template",
            "grpc://localhost:50054/examples.formats.Codec/EchoDocument",
        ]
    )
    assert method.operation == Describe("examples.formats.Codec/EchoDocument")


def test_options_cannot_precede_a_subcommand() -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--proto", "api.proto", "list", "localhost:50051"])

    assert error.value.code == 2


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


def test_curl_style_short_options() -> None:
    command = parse_arguments(
        [
            "grpcs://localhost:50051/example.Echo/Call",
            "-k",
            "-i",
            "-m",
            "2.5",
            "-A",
            "test-client/1.0",
        ]
    )

    assert command.transport.mode == "insecure"
    assert command.diagnostics.show_headers is True
    assert command.timeout == 2.5
    assert command.connection.user_agent == "test-client/1.0"


def test_wait_for_ready_is_preserved_for_invocation() -> None:
    command = parse_arguments(
        ["--wait-for-ready", "grpc://localhost:50051/example.Echo/Call"]
    )

    assert command.invoke_options is not None
    assert command.invoke_options.wait_for_ready is True


def test_wait_for_ready_help_explains_local_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--help"])

    assert error.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert (
        "--wait-for-ready let the RPC wait for readiness until --max-time; "
        "local schemas skip the connection preflight"
    ) in help_text


def test_connection_options_are_preserved_in_command_configuration() -> None:
    command = parse_arguments(
        [
            "--connect-timeout",
            "1.5",
            "--max-recv-message-size",
            "1024",
            "--max-send-message-size",
            "2048",
            "--compression",
            "gzip",
            "--keepalive-time",
            "2.25",
            "--authority",
            "route.example",
            "--servername",
            "certificate.example",
            "grpcs://localhost:50051/example.Echo/Call",
        ]
    )

    assert command.connection.connect_timeout == 1.5
    assert command.connection.max_recv_message_size == 1024
    assert command.connection.max_send_message_size == 2048
    assert command.connection.compression == "gzip"
    assert command.connection.keepalive_time == 2.25
    assert command.transport.authority == "route.example"
    assert command.transport.server_name == "certificate.example"


@pytest.mark.parametrize("data", ["-", "@-"])
def test_data_dash_forms_read_stdin(
    monkeypatch: pytest.MonkeyPatch, data: str
) -> None:
    stream = io.BytesIO(b'{"message":"hello"}')
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))

    with open_input(data) as source:
        assert source is stream
        assert source.read() == b'{"message":"hello"}'


def test_invocation_help_documents_both_stdin_forms(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "- or @- reads stdin" in help_text
    assert "read a file (- for stdin)" in help_text


def test_field_file_dash_selects_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.BytesIO(b"field contents")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
    command = parse_arguments(
        [
            "--field:file",
            "content=-",
            "grpcs://localhost:50051/example.Upload/Send",
        ]
    )

    assert command.invoke_options is not None
    assert command.invoke_options.data == "@-"
    assert command.invoke_options.input_field == "content"
    with open_input(command.invoke_options.data) as source:
        assert source is stream
        assert source.read() == b"field contents"


@pytest.mark.parametrize(
    "argv",
    [
        ["-plaintext", "list", "localhost:50051"],
        ["-proto", "api.proto", "list", "localhost:50051"],
        ["-format", "json", "list", "localhost:50051"],
        ["--plain", "list", "localhost:50051"],
        ["-I", "protos", "list", "localhost:50051"],
        ["--timeout", "2", "grpc://localhost:50051/example.Echo/Call"],
        ["--json-indent", "2", "grpc://localhost:50051/example.Echo/Call"],
        [
            "--preserve-proto-field-names",
            "grpc://localhost:50051/example.Echo/Call",
        ],
        ["--msg-template", "describe", "localhost:50051", "example.Message"],
    ],
)
def test_unsupported_option_spellings_are_rejected(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(argv)

    assert error.value.code == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["localhost:50051"],
        ["describe", "localhost:50051"],
        ["list", "grpc://localhost:50051/example.Echo/Call"],
        ["list", "grpc://localhost:50051/example.Echo", "example.Other"],
        [
            "describe",
            "grpc://localhost:50051/example.Echo/Call",
            "example.Other",
        ],
        ["describe", "grpc://localhost:50051/a/b/c"],
        ["list", "localhost:50051", "Service", "extra"],
        ["localhost:50051/example.Echo/Call"],
        ["grpc://localhost:50051/example.Echo/Call", "list"],
        ["--get-template", "list", "localhost:50051"],
    ],
)
def test_invalid_operation_combinations_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(argv)

    assert error.value.code == 2


def test_help_uses_standard_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--help"])

    captured = capsys.readouterr()
    assert error.value.code == 0
    assert captured.err == ""
    assert "\nExamples:\n  protodial [options...]" in captured.out
    assert "list" in captured.out
    assert "describe" in captured.out
    assert "--msg-template" not in captured.out

    with pytest.raises(SystemExit) as command_help:
        parse_arguments(["describe", "--help"])
    assert command_help.value.code == 0
    assert "--template" in capsys.readouterr().out


def test_missing_operation_prints_examples_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments([])

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert captured.out == ""
    assert "target" in captured.err


@pytest.mark.parametrize(
    "option",
    [
        ["-d", "{}"],
        ["--input-format", "raw"],
        ["--output-format", "raw"],
        ["--field", "name=value"],
        ["--field:file", "content=file.bin"],
        ["--input-field", "value"],
        ["--output-field", "value"],
        ["--chunk-size", "10"],
        ["--wait-for-ready"],
    ],
)
def test_invocation_options_are_rejected_for_discovery(
    option: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["list", *option, "localhost:50051"])

    assert error.value.code == 2


@pytest.mark.parametrize("operation", ["list", "describe"])
def test_rpc_header_is_invocation_only(
    operation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    help_arguments = [operation, "--help"]
    with pytest.raises(SystemExit) as help_exit:
        parse_arguments(help_arguments)
    assert help_exit.value.code == 0
    assert "--rpc-header" not in capsys.readouterr().out

    operands = (
        ["localhost:50051"]
        if operation == "list"
        else ["localhost:50051", "example.Message"]
    )
    with pytest.raises(SystemExit) as parse_exit:
        parse_arguments([operation, "--rpc-header", "x:value", *operands])
    assert parse_exit.value.code == 2


def test_rpc_header_remains_available_for_invocation() -> None:
    command = parse_arguments(
        [
            "--rpc-header",
            "x:value",
            "grpc://localhost:50051/example.Echo/Call",
        ]
    )

    assert command.metadata.rpc_header == ("x:value",)


def test_verbose_is_available_for_discovery_diagnostics() -> None:
    command = parse_arguments(["list", "-vv", "localhost:50051"])

    assert command.diagnostics.verbosity == 2


@pytest.mark.parametrize("option", ["-i", "--show-trailers", "--show-status"])
def test_response_diagnostics_are_rejected_for_discovery(option: str) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["list", option, "localhost:50051"])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "options",
    [
        ["--max-time", "nan"],
        ["--connect-timeout", "inf"],
        ["--keepalive-time", "0.0001"],
        ["--max-recv-message-size", "0"],
        ["--max-send-message-size", "2147483648"],
        ["--chunk-size", "2147483648"],
        ["--max-input-buffer", "2147483648"],
    ],
)
def test_invalid_numeric_options_are_rejected(options: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments([*options, "grpc://localhost:50051/example.Echo/Call"])

    assert error.value.code == 2


def test_template_depth_requires_template_output() -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(
            [
                "describe",
                "--template-depth",
                "2",
                "localhost:50051",
                "example.Message",
            ]
        )

    assert error.value.code == 2

    command = parse_arguments(
        [
            "describe",
            "--template",
            "--template-depth",
            "2",
            "localhost:50051",
            "example.Message",
        ]
    )
    assert command.template_depth == 2


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (["--version"], 0),
        (["localhost:50051"], 2),
        (["list", "--proto", "does-not-exist.proto", "localhost:50051"], 1),
    ],
)
def test_module_exit_codes(arguments: list[str], exit_code: int) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "protodial", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == exit_code


def test_main_dispatches_discovery_payload_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        cli, "list_symbols", lambda _command, _grpc: print("example.Echo")
    )

    assert cli.main(["list", "localhost:50051"]) == 0
    assert all(Path(path).is_dir() for path in default_proto_paths())
    captured = capsys.readouterr()
    assert captured.out == "example.Echo\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("arguments", "require_channel"),
    [
        (
            [
                "--proto",
                "local.proto",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            True,
        ),
        (["list", "localhost:50051"], False),
    ],
)
def test_connection_events_precede_invocation_and_reflection_schema(
    arguments: list[str],
    require_channel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protodial.presentation.diagnostics.context import use_sink
    from protodial.presentation.diagnostics.models import (
        RecordingDiagnosticSink,
    )

    @contextlib.contextmanager
    def ready_channel(*_args: object, **_kwargs: object):
        yield object()

    source = object()
    monkeypatch.setattr(transport, "ready_channel", ready_channel)
    monkeypatch.setattr(
        schema_context,
        "build_descriptor_source",
        lambda *_args, **_kwargs: source,
    )
    command = parse_arguments(arguments)
    sink = RecordingDiagnosticSink()

    with use_sink(sink):
        with schema_context.open_descriptor_session(
            command,
            SimpleNamespace(),
            require_channel=require_channel,
        ) as session:
            assert session.source is source

    names = [event.name for event in sink.events]
    assert names == [
        "transport.connecting",
        "transport.connected",
        "schema.source",
    ]


@pytest.mark.parametrize(
    ("arguments", "expected_await_ready", "expected_connected"),
    [
        (
            [
                "--proto",
                "local.proto",
                "--wait-for-ready",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            False,
            False,
        ),
        (
            [
                "--proto",
                "local.proto",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            True,
            True,
        ),
        (
            [
                "--proto",
                "local.proto",
                "--use-reflection",
                "--wait-for-ready",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            True,
            True,
        ),
    ],
)
def test_wait_for_ready_skips_preflight_only_for_local_invocation(
    arguments: list[str],
    expected_await_ready: bool,
    expected_connected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protodial.presentation.diagnostics.context import use_sink
    from protodial.presentation.diagnostics.models import (
        RecordingDiagnosticSink,
    )

    observed: list[bool] = []

    @contextlib.contextmanager
    def ready_channel(
        *_args: object, await_ready: bool = True
    ) -> Iterator[object]:
        observed.append(await_ready)
        yield object()

    monkeypatch.setattr(transport, "ready_channel", ready_channel)
    monkeypatch.setattr(
        schema_context,
        "build_descriptor_source",
        lambda *_args, **_kwargs: object(),
    )

    sink = RecordingDiagnosticSink()
    with use_sink(sink):
        with schema_context.open_descriptor_session(
            parse_arguments(arguments), SimpleNamespace(), require_channel=True
        ):
            pass

    assert observed == [expected_await_ready]
    names = [event.name for event in sink.events]
    assert ("transport.connected" in names) is expected_connected


@pytest.mark.parametrize("arguments", [["--help"], ["--version"]])
def test_main_silences_closed_pipe_during_parser_stdout(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    class ClosedStdout:
        def write(self, _value: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", ClosedStdout())
    monkeypatch.setattr(cli, "_silence_broken_stdout", lambda: None)

    assert cli.main(arguments) == 0


@pytest.mark.parametrize("operation", ["list", "invoke"])
def test_main_does_not_assume_unattributed_broken_pipe_is_stdout(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=Exception), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "_silence_broken_stdout", lambda: None)

    def closed_pipe(*_args: object) -> None:
        raise BrokenPipeError

    if operation == "list":
        monkeypatch.setattr(cli, "list_symbols", closed_pipe)
        arguments = ["list", "localhost:50051"]
    else:
        monkeypatch.setattr(cli, "invoke", closed_pipe)
        arguments = ["grpc://localhost:50051/example.Echo/Call"]

    assert cli.main(arguments) == 1


def test_main_flushes_stdout_inside_closed_pipe_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BufferedClosedStdout:
        def __init__(self) -> None:
            self.value = ""

        def write(self, value: str) -> int:
            self.value += value
            return len(value)

        def flush(self) -> None:
            raise BrokenPipeError

    stdout = BufferedClosedStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cli, "_silence_broken_stdout", lambda: None)

    assert cli.main(["--version"]) == 0
    assert stdout.value.startswith("protodial ")


def test_main_preserves_failed_command_exit_when_final_flush_hits_epipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )

    class BufferedClosedStdout:
        def flush(self) -> None:
            raise BrokenPipeError

    def fail(_command: Command, _grpc: object) -> None:
        raise CliError("primary command failure")

    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "describe", fail)
    monkeypatch.setattr(sys, "stdout", BufferedClosedStdout())
    monkeypatch.setattr(cli, "_silence_broken_stdout", lambda: None)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_main_reports_non_epipe_final_flush_as_local_io(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    diagnostic_format: str,
) -> None:
    class FullStdout:
        def __init__(self) -> None:
            self.value = ""

        def write(self, value: str) -> int:
            self.value += value
            return len(value)

        def flush(self) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    stdout = FullStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cli, "_silence_broken_stdout", lambda: None)
    arguments = ["--version"]
    if diagnostic_format == "json":
        monkeypatch.setenv("PROTODIAL_DIAGNOSTIC_FORMAT", "json")

    assert cli.main(arguments) == 1
    stderr = capsys.readouterr().err
    assert stdout.value.startswith("protodial ")
    if diagnostic_format == "json":
        value = json.loads(stderr)
        assert value["event"] == "local_io.failed"
        assert value["code"] == "E8002"
        assert value["context"] == {
            "operation": "flush",
            "stream": "standard output",
            "errno": errno.ENOSPC,
        }
        assert value["causes"] == ["No space left on device"]
    else:
        assert "protodial: (E8002) could not flush standard output" in stderr
        assert "No space left on device" in stderr
        assert "available disk space" in stderr
    assert "Traceback" not in stderr


def test_main_contains_stderr_failure_while_reporting_final_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    silenced = []

    class FullStdout:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            raise OSError(errno.ENOSPC, "disk full")

    class FailedStderr:
        def isatty(self) -> bool:
            return False

        def write(self, _value: str) -> int:
            raise OSError(errno.EIO, "stderr unavailable")

        def flush(self) -> None:
            raise OSError(errno.EIO, "stderr unavailable")

    monkeypatch.setattr(sys, "stdout", FullStdout())
    monkeypatch.setattr(sys, "stderr", FailedStderr())
    monkeypatch.setattr(cli, "_silence_broken_stdout", lambda: None)
    monkeypatch.setattr(
        cli, "_silence_broken_stderr", lambda: silenced.append(True)
    )

    assert cli.main(["--version"]) == 1
    assert silenced == [True]


@pytest.mark.parametrize(
    ("name", "silence"),
    [
        ("stdout", cli._silence_broken_stdout),
        ("stderr", cli._silence_broken_stderr),
    ],
)
def test_silencing_replaces_standard_stream_without_a_file_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    silence: object,
) -> None:
    class FailedStream:
        def fileno(self) -> int:
            raise OSError(errno.EBADF, "bad file descriptor")

        def flush(self) -> None:
            raise OSError(errno.EIO, "stream unavailable")

    failed = FailedStream()
    monkeypatch.setattr(sys, name, failed)

    assert callable(silence)
    silence()

    replacement = getattr(sys, name)
    assert replacement is not failed
    replacement.flush()


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
@pytest.mark.parametrize("credential", ["cacert", "cert", "key"])
def test_main_reports_tls_file_read_failures_as_local_io(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    credential: str,
    diagnostic_format: str,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    missing = tmp_path / f"missing-{credential}.pem"
    existing = tmp_path / "existing.pem"
    existing.write_bytes(b"placeholder")
    target = "grpcs://localhost:50051/example.Echo/Call"
    option = "--" + credential
    if credential == "cacert":
        arguments = [
            option,
            str(existing),
            option,
            str(missing),
            target,
        ]
    elif credential == "cert":
        arguments = [
            option,
            str(missing),
            "--key",
            str(existing),
            target,
        ]
    else:
        arguments = [
            "--cert",
            str(existing),
            option,
            str(missing),
            target,
        ]
    if diagnostic_format == "json":
        arguments[0:0] = ["--diagnostic-format", "json"]

    def validate(command: Command, _grpc: object) -> None:
        transport.validate_tls_material(command)

    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "invoke", validate)

    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    argument_number = arguments.index(str(missing)) + 1
    if diagnostic_format == "json":
        value = json.loads(captured.err)
        assert value["event"] == "local_io.failed"
        assert value["code"] == "E8002"
        assert value["context"]["path"] == str(missing)
        assert value["context"]["argument"] == option
        assert value["spans"] == [
            {
                "source": f"argument {argument_number}",
                "text": str(missing),
                "start": 0,
                "end": len(str(missing)),
                "label": "path could not be read",
            }
        ]
        assert value["causes"] == ["No such file or directory"]
        assert "exists and is readable" in value["help"][0]
    else:
        assert "protodial: (E8002) could not read" in captured.err
        assert "protodial: (E3002)" not in captured.err
        assert missing.name in captured.err
        assert f"--> argument {argument_number}" in captured.err
        assert "path could not be read" in captured.err
        normalized = " ".join(captured.err.split())
        assert "exists and is readable, then pass it" in normalized
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("credential", ["cert", "key"])
@pytest.mark.parametrize("attached", [False, True])
def test_scalar_tls_file_error_locates_last_identical_occurrence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    credential: str,
    attached: bool,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    missing = str(tmp_path / f"missing-{credential}.pem")
    existing = tmp_path / "existing.pem"
    existing.write_bytes(b"placeholder")
    option = "--" + credential
    repeated = (
        [f"{option}={missing}", f"{option}={missing}"]
        if attached
        else [option, missing, option, missing]
    )
    other = "--key" if credential == "cert" else "--cert"
    arguments = [
        "--diagnostic-format",
        "json",
        *repeated,
        other,
        str(existing),
        "grpcs://localhost:50051/example.Echo/Call",
    ]

    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        cli,
        "invoke",
        lambda command, _grpc: transport.validate_tls_material(command),
    )

    assert cli.main(arguments) == 1
    value = json.loads(capsys.readouterr().err)
    active_token = repeated[-1]
    argument_number = len(arguments) - arguments[::-1].index(active_token)
    assert value["spans"] == [
        {
            "source": f"argument {argument_number}",
            "text": active_token,
            "start": len(option) + 1 if attached else 0,
            "end": len(active_token) if attached else len(missing),
            "label": "path could not be read",
        }
    ]


def test_repeatable_cacert_error_keeps_first_identical_occurrence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    missing = str(tmp_path / "missing-ca.pem")
    arguments = [
        "--diagnostic-format",
        "json",
        "--cacert",
        missing,
        "--cacert",
        missing,
        "grpcs://localhost:50051/example.Echo/Call",
    ]
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        cli,
        "invoke",
        lambda command, _grpc: transport.validate_tls_material(command),
    )

    assert cli.main(arguments) == 1
    value = json.loads(capsys.readouterr().err)
    assert value["spans"][0]["source"] == "argument 4"


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
@pytest.mark.parametrize(
    ("credential", "attached", "expected_argument"),
    [
        ("cacert", False, 4),
        ("cacert", True, 3),
        ("cert", False, 6),
        ("cert", True, 4),
    ],
)
def test_empty_tls_material_keeps_option_path_and_configuration_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    diagnostic_format: str,
    credential: str,
    attached: bool,
    expected_argument: int,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    empty = tmp_path / f"empty-{credential}.pem"
    empty.write_bytes(b" \n")
    option = "--" + credential
    repeated = (
        [f"{option}={empty}", f"{option}={empty}"]
        if attached
        else [option, str(empty), option, str(empty)]
    )
    arguments = ["--diagnostic-format", diagnostic_format, *repeated]
    if credential == "cert":
        key = tmp_path / "client.key"
        key.write_bytes(b"placeholder")
        arguments.extend(("--key", str(key)))
    arguments.append("grpcs://localhost:50051/example.Echo/Call")

    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        cli,
        "invoke",
        lambda command, _grpc: transport.validate_tls_material(command),
    )

    assert cli.main(arguments) == 1
    rendered = capsys.readouterr().err
    if diagnostic_format == "json":
        value = json.loads(rendered)
        assert (value["event"], value["code"]) == (
            "command.failed",
            "E2001",
        )
        assert value["context"]["kind"] == "empty"
        assert value["context"]["path"] == str(empty)
        assert value["context"]["argument"] == option
        assert value["spans"][0]["source"] == (f"argument {expected_argument}")
        assert value["spans"][0]["end"] <= len(value["spans"][0]["text"])
        assert value["causes"] == ["the file contains no non-whitespace data"]
        assert "valid PEM data" in value["help"][0]
    else:
        assert "protodial: (E2001)" in rendered
        assert "protodial: (E3002)" not in rendered
        assert "protodial: (E9001)" not in rendered
        assert f"--> argument {expected_argument}" in rendered
        assert "invalid TLS material" in rendered
        assert "reason: the file contains no non-whitespace data" in rendered
        assert "help: write valid PEM data" in rendered


@pytest.mark.parametrize(
    ("kind", "option", "option_value", "argument", "description"),
    [
        ("proto", "--proto", "{path}", "--proto", "proto source file"),
        ("protoset", "--protoset", "{path}", "--protoset", "protoset file"),
        ("data", "--data", "@{path}", "--data", "request data file"),
        (
            "field",
            "--field:file",
            "content={path}",
            "--field:file",
            "field file",
        ),
    ],
)
@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_local_input_errors_are_typed_and_exactly_located(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kind: str,
    option: str,
    option_value: str,
    argument: str,
    description: str,
    attached: bool,
    diagnostic_format: str,
) -> None:
    from protodial.schema.local import local_descriptor_source

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    path = str(tmp_path / f"missing-{kind}")
    value_text = option_value.format(path=path)
    supplied = [f"{option}={value_text}"] if attached else [option, value_text]
    if kind in {"proto", "protoset"}:
        arguments = [
            "list",
            "--diagnostic-format",
            diagnostic_format,
            *supplied,
            "unused.invalid",
        ]

        def fail(command: Command, _grpc: object) -> None:
            local_descriptor_source(command)

        monkeypatch.setattr(cli, "list_symbols", fail)
    else:
        arguments = [
            "--diagnostic-format",
            diagnostic_format,
            *supplied,
            "grpc://localhost:50051/example.Echo/Call",
        ]

        def fail(command: Command, _grpc: object) -> None:
            assert command.invoke_options is not None
            options = command.invoke_options
            with open_input(
                options.data,
                argument=options.data_argument,
                option_strings=options.data_option_strings,
                option_value=options.data_option_value,
            ):
                pass

        monkeypatch.setattr(cli, "invoke", fail)
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    token = supplied[0] if attached else value_text
    path_start = token.rfind(path)
    source = f"argument {arguments.index(token) + 1}"
    if diagnostic_format == "json":
        result = json.loads(captured.err)
        assert result["event"] == "local_io.failed"
        assert result["code"] == "E8002"
        assert result["message"] == f"could not read {description} `{path}`"
        assert result["context"]["operation"] == "read"
        assert result["context"]["path"] == path
        assert result["context"]["argument"] == argument
        assert result["causes"] == ["No such file or directory"]
        assert result["spans"] == [
            {
                "source": source,
                "text": token,
                "start": path_start,
                "end": path_start + len(path),
                "label": "path could not be read",
            }
        ]
    else:
        assert (
            f"protodial: (E8002) could not read {description}" in captured.err
        )
        assert f"--> {source}" in captured.err
        assert Path(path).name in captured.err
        assert "No such file or directory" in captured.err
        assert "exists and is readable" in captured.err
    assert "/protodial-" not in captured.err


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_descriptor_export_error_is_typed_located_and_hides_temporary_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    attached: bool,
    diagnostic_format: str,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    path = str(tmp_path / "schema.pb")
    temporary = str(tmp_path / ".schema.pb.random")
    supplied = (
        [f"--protoset-out={path}"] if attached else ["--protoset-out", path]
    )
    arguments = [
        "--diagnostic-format",
        diagnostic_format,
        *supplied,
        "grpc://localhost:50051/example.Echo/Call",
    ]

    def export_failure(_source: object, _path: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", temporary)

    def fail(_command: Command, _grpc: object) -> None:
        with _export_after_use(SimpleNamespace(), path):
            pass

    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "invoke", fail)
    monkeypatch.setattr(schema_context, "write_descriptor_set", export_failure)

    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    token = supplied[0] if attached else path
    start = token.rfind(path)
    source = f"argument {arguments.index(token) + 1}"
    if diagnostic_format == "json":
        result = json.loads(captured.err)
        assert result["event"] == "descriptor.export_failed"
        assert result["code"] == "E8002"
        assert result["context"] == {
            "operation": "write",
            "path": path,
            "argument": "--protoset-out",
            "errno": errno.EACCES,
        }
        assert result["causes"] == ["Permission denied"]
        assert result["spans"] == [
            {
                "source": source,
                "text": token,
                "start": start,
                "end": start + len(path),
                "label": "export destination could not be written",
            }
        ]
        assert "writable" in result["help"][0]
    else:
        assert (
            "protodial: (E8002) could not export descriptor set" in captured.err
        )
        assert f"--> {source}" in captured.err
        assert Path(path).name in captured.err
        assert "Permission denied" in captured.err
        assert "writable" in captured.err
    assert temporary not in captured.err


def test_main_writes_domain_errors_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(_command: Command, _grpc: object) -> None:
        raise CliError("schema unavailable")

    monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "protodial: (E2001) schema unavailable\n"


def test_main_escapes_controls_in_reflection_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(_command: Command, _grpc: object) -> None:
        raise CliError("reflection: bad\nforged\rline\x1b[2J\u202eowned")

    monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "protodial: (E2001) reflection: "
        "bad\\nforged\\rline\\x1b[2J\\u202eowned\n"
    )
    assert "\x1b" not in captured.err


@pytest.mark.parametrize("input_format", list(MessageFormat))
@pytest.mark.parametrize(
    ("client_streaming", "expected_count"), [(False, 1), (True, 0)]
)
def test_main_resolves_omitted_input_after_method_cardinality_is_known(
    monkeypatch: pytest.MonkeyPatch,
    input_format: MessageFormat,
    client_streaming: bool,
    expected_count: int,
) -> None:
    from google.protobuf import descriptor_pool, empty_pb2

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def inspect_input(command: Command, _grpc: object) -> None:
        options = command.invoke_options
        assert options is not None
        requests = iter_requests(
            io.BytesIO(),
            options,
            SimpleNamespace(
                client_streaming=client_streaming, server_streaming=False
            ),
            empty_pb2.Empty,
            descriptor_pool.Default(),
            "value" if input_format is MessageFormat.RAW else None,
        )
        assert list(requests) == [empty_pb2.Empty()] * expected_count

    monkeypatch.setattr(cli, "invoke", inspect_input)

    assert (
        cli.main(
            [
                "--input-format",
                input_format.value,
                "grpc://localhost:50051/example.Echo/Call",
            ]
        )
        == 0
    )


@pytest.mark.parametrize("primary_kind", ["rpc", "cli"])
def test_main_reports_secondary_export_failure_without_replacing_primary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    primary_kind: str,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def export_failure(_source: object, _path: str) -> None:
        error = OSError("disk full\nforged")
        setattr(
            error,
            "_protodial_notes",
            ("descriptor temporary-file cleanup also failed: unlink",),
        )
        raise error

    monkeypatch.setattr(schema_context, "write_descriptor_set", export_failure)
    if primary_kind == "rpc":
        summary = RpcSummary(
            [], [], SimpleNamespace(name="INTERNAL"), "primary", 0, 0, 0.1
        )
        primary: BaseException = RpcFailure(
            FakeRpcError("primary"), summary, False
        )
        argv = ["grpc://localhost:50051/example.Echo/Call"]

        def fail(_command: Command, _grpc: object) -> None:
            with _export_after_use(SimpleNamespace(), "output.pb"):
                raise primary

        monkeypatch.setattr(cli, "invoke", fail)
    else:
        primary = CliError("primary")
        argv = ["describe", "localhost:50051", "example.Echo"]

        def fail(_command: Command, _grpc: object) -> None:
            with _export_after_use(SimpleNamespace(), "output.pb"):
                raise primary

        monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert "primary" in captured.err
    assert "note: descriptor export also failed: disk full\\nforged" in (
        captured.err
    )
    assert "descriptor temporary-file cleanup also failed:" in captured.err
    assert "unlink" in captured.err
    assert captured.err.index("primary") < captured.err.index(
        "descriptor export also failed"
    )
    assert getattr(primary, "_protodial_notes") == (
        "descriptor export also failed: disk full\nforged",
        "descriptor export also failed detail: descriptor temporary-file "
        "cleanup also failed: unlink",
    )


def test_main_reports_unexpected_type_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(_command: Command, _grpc: object) -> None:
        raise TypeError("internal defect")

    monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("protodial: (E9001) internal defect")
    assert "Traceback" not in captured.err


def test_main_does_not_repeat_status_already_rendered_by_invocation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    summary = RpcSummary(
        [], [], SimpleNamespace(name="INTERNAL"), "failed", 1, 0, 0.1
    )

    def fail(_command: Command, _grpc: object) -> None:
        raise RpcFailure(FakeRpcError("failed"), summary, status_rendered=True)

    monkeypatch.setattr(cli, "invoke", fail)

    assert (
        cli.main(["--show-status", "grpc://localhost:50051/example.Echo/Call"])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("INTERNAL") == 1
    assert "gRPC INTERNAL" not in captured.err


def test_main_maps_keyboard_interrupt_to_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def interrupt(_command: Command, _grpc: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "invoke", interrupt)

    assert cli.main(["grpc://localhost:50051/example.Echo/Call"]) == 130


def test_main_keeps_interrupted_status_single_and_non_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from protodial.presentation.diagnostics.context import current_sink
    from protodial.presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
    )

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def interrupt(_command: Command, _grpc: object) -> None:
        sink = current_sink()
        assert sink is not None
        sink.emit(
            DiagnosticEvent(
                "rpc.completed",
                level=0,
                severity=DiagnosticSeverity.WARNING,
                code="W4101",
                fields={
                    "status": "CANCELLED",
                    "sent": 1,
                    "received": 0,
                },
            )
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "invoke", interrupt)

    assert (
        cli.main(
            [
                "--show-status",
                "grpc://localhost:50051/example.Echo/Call",
            ]
        )
        == 130
    )
    captured = capsys.readouterr()
    assert captured.err.count("CANCELLED") == 1
    assert captured.err == "* CANCELLED; sent 1 message; received 0 messages\n"
    assert "error" not in captured.err


def test_target_url_rejects_invalid_path_and_query() -> None:
    with pytest.raises(CliError, match="/SERVICE/METHOD"):
        parse_target("grpc://localhost:50051/example.Greeter")
    with pytest.raises(CliError, match="query"):
        parse_target("grpc://localhost:50051/example.Greeter/SayHello?x=1")


def test_parse_metadata_decodes_binary_values() -> None:
    assert parse_metadata(["Token: value", "trace-bin: YWJj"]) == [
        ("token", "value"),
        ("trace-bin", b"abc"),
    ]


def test_parse_metadata_rejects_malformed_values() -> None:
    with pytest.raises(CliError, match="key:value"):
        parse_metadata(["missing-separator"])


def test_read_exact_reads_framed_payload() -> None:
    source = io.BytesIO(struct.pack(">I", 3) + b"abc")

    assert read_exact(source, 4) == struct.pack(">I", 3)
    assert read_exact(source, 3) == b"abc"
    assert read_exact(source, 1, allow_eof=True) is None


def test_read_exact_rejects_truncated_payload() -> None:
    with pytest.raises(CliError, match="truncated"):
        read_exact(io.BytesIO(b"ab"), 3)
