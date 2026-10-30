from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from protodial import cli


@pytest.mark.parametrize("operation", ["list", "describe"])
def test_discovery_rpc_header_is_a_structured_usage_error(
    operation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    operands = (
        ["localhost:50051"]
        if operation == "list"
        else ["localhost:50051", "example.Message"]
    )
    assert (
        cli.main(
            [
                operation,
                "--diagnostic-format",
                "json",
                "--rpc-header",
                "x:value",
                *operands,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "usage.invalid"
    assert value["code"] == "E1001"


def test_missing_runtime_dependencies_use_bootstrapped_json_diagnostic(
    tmp_path: Path,
) -> None:
    # Expose the installed package while -S hides all third-party dependencies.
    shutil.copytree(
        Path(cli.__file__).resolve().parent,
        tmp_path / "protodial",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "from protodial.cli import main; "
                "raise SystemExit(main(['list', "
                "'--diagnostic-format', 'json', 'localhost:1']))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    value = json.loads(completed.stderr)
    assert value["event"] == "dependency.unavailable"
    assert value["code"] == "E8001"
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("module", "distribution"),
    [
        ("cryptography.hazmat.primitives", "cryptography"),
        ("grpc_reflection.v1alpha", "grpcio-reflection"),
    ],
)
def test_missing_declared_dependency_is_actionable(
    module: str,
    distribution: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rpc_error = type("RpcError", (Exception,), {})
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=rpc_error), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    monkeypatch.setattr(cli, "describe", fail)

    assert (
        cli.main(
            [
                "describe",
                "--diagnostic-format",
                "json",
                "localhost:50051",
                "example.Echo",
            ]
        )
        == 1
    )
    value = json.loads(capsys.readouterr().err)
    assert value["event"] == "dependency.unavailable"
    assert value["code"] == "E8001"
    assert distribution in value["message"]


def test_closed_stderr_preserves_usage_exit_in_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; os.close(2); "
                "from protodial.cli import main; "
                "raise SystemExit(main(['--not-an-option']))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_closed_stderr_preserves_direct_main_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedStderr:
        def write(self, _value: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stderr", ClosedStderr())

    assert cli.main(["--not-an-option"]) == 2


def test_verbose_local_discovery_does_not_claim_a_connection(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    proto = tmp_path / "offline.proto"
    proto.write_text(
        'syntax = "proto3"; package example; service Offline {}\n',
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "list",
                "-vv",
                "--diagnostic-format",
                "json",
                "--proto",
                str(proto),
                "unused.invalid",
            ]
        )
        == 0
    )
    captured = capfd.readouterr()
    events = [json.loads(line)["event"] for line in captured.err.splitlines()]
    assert captured.out == "example.Offline\n"
    assert "schema.source" in events
    assert "transport.connecting" not in events
    assert "transport.connected" not in events


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_runtime_initialization_defects_are_structured_without_traceback(
    diagnostic_format: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_runtime() -> object:
        raise RuntimeError("runtime initialization defect")

    monkeypatch.setattr(cli, "load_runtime", fail_runtime)

    assert (
        cli.main(
            [
                "describe",
                "--diagnostic-format",
                diagnostic_format,
                "localhost:50051",
                "example.Echo",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "E9001" in captured.err
    assert "runtime initialization defect" in captured.err
    assert "Traceback" not in captured.err


def test_runtime_initialization_interrupt_maps_to_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_runtime() -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "load_runtime", interrupt_runtime)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 130
