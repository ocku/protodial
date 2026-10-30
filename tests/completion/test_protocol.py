from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import protodial.cli as cli_module
import protodial.completion.engine as completion_engine
from protodial.cli import main
from protodial.completion.command import (
    run_completion_command,
    run_completion_protocol,
)
from protodial.completion.engine import Candidate, complete

from .support import _saved_proto


def test_continuation_candidates_suppress_trailing_space(
    tmp_path: Path,
) -> None:
    environment, proto = _saved_proto(tmp_path)
    field = complete(
        [
            "--proto",
            str(proto),
            "--method",
            "saved.example.Greeter/SayHello",
            "--field",
            "mo",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    mood = next(item for item in field if item.value == "mood=")
    assert mood.append_space is False
    directory = tmp_path / "nested"
    directory.mkdir()
    paths = complete(["--proto", "ne"], environment=environment, cwd=tmp_path)
    nested = next(item for item in paths if item.value == "nested/")
    assert nested.append_space is False


def test_completion_scripts_use_nul_protocol_without_eval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_completion_command(["bash"]) == 0
    bash = capsys.readouterr().out
    assert "read -r -d ''" in bash
    assert "__complete" in bash
    assert "compopt" not in bash
    assert "complete -o nospace" in bash
    assert "eval" not in bash

    assert run_completion_command(["zsh"]) == 0
    zsh = capsys.readouterr().out
    assert "read -r -d ''" in zsh
    assert "compadd -l" in zsh
    assert "eval" not in zsh


def test_private_protocol_is_nul_delimited(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        completion_engine,
        "complete",
        lambda *_args, **_kwargs: [Candidate("a b", "two words")],
    )
    assert (
        run_completion_protocol(
            ["--protocol=protodial-v1", "--shell", "bash", "--", ""]
        )
        == 0
    )
    assert capfd.readouterr().out == ("a b\0a b\n\u21b3 two words\0" + "1\0")


def test_private_protocol_forwards_cursor_index(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_complete(*_args: object, **kwargs: object) -> list[Candidate]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(completion_engine, "complete", fake_complete)
    monkeypatch.chdir(tmp_path)
    assert (
        run_completion_protocol(
            [
                "--protocol=protodial-v1",
                "--shell",
                "bash",
                "--cursor",
                "1",
                "--",
                "one",
                "two",
            ]
        )
        == 0
    )
    capfd.readouterr()
    assert observed["cursor"] == 1


def test_private_protocol_accepts_nul_words_on_stdin() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "protodial",
            "__complete",
            "--protocol=protodial-v1",
            "--shell",
            "bash",
            "--cursor",
            "1",
            "--words-stdin",
        ],
        input=b"--compression\0g\0--color\0never\0",
        capture_output=True,
        check=True,
    )
    assert completed.stdout.split(b"\0") == [
        b"gzip",
        "gzip\n\u21b3 compression value".encode(),
        b"1",
        b"",
    ]


def test_native_target_named_private_command_uses_ordinary_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: object, *_args: object) -> int:
        observed["command"] = command
        return 0

    monkeypatch.setattr(cli_module, "_run", fake_run)
    assert main(["__complete", "--method", "example.API/Call"]) == 0
    command = observed["command"]
    assert command.target.channel == "__complete"  # type: ignore[attr-defined]
    assert command.operation.method == "example.API/Call"  # type: ignore[attr-defined]


def test_native_target_named_completion_uses_ordinary_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: object, *_args: object) -> int:
        observed["command"] = command
        return 0

    monkeypatch.setattr(cli_module, "_run", fake_run)
    assert main(["completion", "--method", "example.API/Call"]) == 0
    command = observed["command"]
    assert command.target.channel == "completion"  # type: ignore[attr-defined]
    assert command.operation.method == "example.API/Call"  # type: ignore[attr-defined]


def test_public_completion_management_grammar_still_dispatches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["completion", "bash"]) == 0
    assert "_protodial_complete" in capsys.readouterr().out
