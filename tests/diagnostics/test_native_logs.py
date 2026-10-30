from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from protodial import cli


def test_invalid_diagnostic_environment_has_plain_usage_error(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PROTODIAL_DIAGNOSTIC_FORMAT", "xml")
    assert cli.main(["--version"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("protodial: (E1002) ")
    assert "\x1b" not in captured.err


def test_json_mode_suppresses_native_grpc_stderr_before_runtime_load(
    monkeypatch, capsys
) -> None:
    observed: list[str | None] = []
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=Exception), protobuf_errors=()
    )

    def load_runtime() -> object:
        observed.append(os.environ.get("GRPC_VERBOSITY"))
        return runtime

    monkeypatch.setenv("GRPC_VERBOSITY", "DEBUG")
    monkeypatch.setattr(cli, "load_runtime", load_runtime)
    monkeypatch.setattr(cli, "describe", lambda *_args: None)

    assert (
        cli.main(
            [
                "describe",
                "--diagnostic-format",
                "json",
                "localhost:50051",
                "example.Message",
            ]
        )
        == 0
    )
    assert observed == ["NONE"]
    assert capsys.readouterr().err == ""


def test_human_mode_suppresses_native_logs_and_escapes_malicious_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[str | None] = []
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=type("RpcError", (Exception,), {})),
        protobuf_errors=(),
    )

    def load_runtime() -> object:
        observed.append(os.environ.get("GRPC_VERBOSITY"))
        return runtime

    def fail(*_args: object) -> None:
        raise ConnectionError("dial failed")

    monkeypatch.setenv("GRPC_VERBOSITY", "DEBUG")
    monkeypatch.setattr(cli, "load_runtime", load_runtime)
    monkeypatch.setattr(cli, "invoke", fail)
    target = "grpc://bad\x1bX.example:50051/example.Echo/Call"

    assert cli.main(["--color", "never", target]) == 1
    captured = capsys.readouterr()
    assert observed == ["NONE"]
    assert captured.out == ""
    assert "\\x1bX" in captured.err
    assert "\x1b" not in captured.err
