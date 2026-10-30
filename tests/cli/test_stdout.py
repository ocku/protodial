import errno
import json
import sys
from types import SimpleNamespace

import pytest

import protodial.cli as cli
from protodial.commands.models import Command
from protodial.errors import CliError

from .support import FakeRpcError


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
