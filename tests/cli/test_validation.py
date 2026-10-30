import pytest

from protodial.argument_parser import parse_arguments


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
