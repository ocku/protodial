import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import protodial.cli as cli
import protodial.transport as transport
from protodial.commands.models import Command

from .support import FakeRpcError


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
