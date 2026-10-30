from pathlib import Path

import pytest

from protodial.argument_parser import parse_arguments
from protodial.commands.models import Command
from protodial.commands.operations import Describe, Invoke, ListSymbols
from protodial.formats import MessageFormat
from protodial.schema.paths import (
    default_proto_paths,
    ensure_default_proto_paths,
)


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
