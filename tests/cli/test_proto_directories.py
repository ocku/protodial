import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import protodial.cli as cli
from protodial.commands.models import Command
from protodial.schema.paths import default_proto_paths

from .support import FakeRpcError


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
