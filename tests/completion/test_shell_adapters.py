from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .support import _completion_script, _fake_protodial


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_adapter_honors_cursor_and_nospace(
    tmp_path: Path, shell: str
) -> None:
    if os.name == "nt":
        pytest.skip("Bash and Zsh adapters require a Unix-like shell")
    shell_executable = shutil.which(shell)
    if shell_executable is None:
        pytest.skip(f"{shell} is unavailable")
    _executable, log, input_log = _fake_protodial(tmp_path)
    script = _completion_script(tmp_path, shell)
    if shell == "bash":
        program = """
source "$1"
COMP_WORDS=(protodial --field '' --key-password supersecret \
    grpc://later.example:443)
COMP_CWORD=2
_protodial_complete
printf 'VALUE=<%s>\n' "${COMPREPLY[0]}"
export PROTODIAL_TEST_MODE=value
COMP_WORDS=(protodial --field 'mood=' --key-password supersecret \
    grpc://later.example:443)
COMP_CWORD=2
_protodial_complete
printf 'ENUM=<%s>\n' "${COMPREPLY[0]}"
export PROTODIAL_TEST_MODE=array
COMP_WORDS=(protodial --field 'moods=' grpc://later.example:443)
COMP_CWORD=2
_protodial_complete
printf 'ARRAY=<%s>\n' "${COMPREPLY[0]}"
"""
    else:
        program = """
source "$1"
path=("$2" $path)
rehash
compadd() { for value in "$@"; do print -r -- "ARG=<$value>"; done; }
words=(protodial --field '' --key-password supersecret \
    grpc://later.example:443)
CURRENT=3
_protodial
"""
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROTODIAL_TEST_LOG": str(log),
        "PROTODIAL_TEST_INPUT": str(input_log),
    }
    completed = subprocess.run(
        [
            shell_executable,
            "-c",
            program,
            "completion-test",
            str(script),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=True,
    )
    if shell == "bash":
        assert "VALUE=<mood=>" in completed.stdout
        assert "ENUM=<MOOD_HAPPY >" in completed.stdout
        assert 'ARRAY=<\\[\\"MOOD_HAPPY\\"\\] >' in completed.stdout
    else:
        assert "ARG=<-S>" in completed.stdout
        assert "ARG=<mood=>" in completed.stdout
    arguments = log.read_text(encoding="utf-8").splitlines()
    assert arguments[:5] == [
        "__complete",
        "--protocol=protodial-v1",
        "--shell",
        shell,
        "--cursor",
    ]
    assert arguments[5] == "1"
    assert arguments[-1] == "--words-stdin"
    assert "supersecret" not in arguments
    words = input_log.read_bytes().split(b"\0")
    if shell == "bash":
        assert words[:-1] == [
            b"--field",
            b"moods=",
            b"grpc://later.example:443",
        ]
    else:
        assert words[:-1] == [
            b"--field",
            b"",
            b"--key-password",
            b"supersecret",
            b"grpc://later.example:443",
        ]


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("word_style", ["plain", "quoted", "escaped"])
def test_shell_adapter_truncates_the_active_word_at_cursor(
    tmp_path: Path, shell: str, word_style: str
) -> None:
    if os.name == "nt":
        pytest.skip("Bash and Zsh adapters require a Unix-like shell")
    shell_executable = shutil.which(shell)
    if shell_executable is None:
        pytest.skip(f"{shell} is unavailable")
    script = _completion_script(tmp_path, shell)
    log = tmp_path / "cursor-words.bin"
    executable = tmp_path / "protodial"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, subprocess, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "pathlib.Path(os.environ['PROTODIAL_CURSOR_LOG']).write_bytes(data)\n"
        "result = subprocess.run(\n"
        "    [sys.executable, '-m', 'protodial', *sys.argv[1:]],\n"
        "    input=data, stdout=subprocess.PIPE, check=False)\n"
        "sys.stdout.buffer.write(result.stdout)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    if word_style == "quoted":
        line = 'protodial "--compression=gBAD extra" --header "later value"'
        left = 'protodial "--compression=g'
        words = [
            "protodial",
            "--compression=gBAD extra",
            "--header",
            "later value",
        ]
        active = 1
    elif word_style == "escaped":
        line = "protodial --compression=\\gBAD --header later\\ value"
        left = "protodial --compression=\\g"
        words = [
            "protodial",
            "--compression",
            "=",
            "gBAD",
            "--header",
            "later value",
        ]
        active = 3
    else:
        line = 'protodial --compression=gBAD --header "later value"'
        left = "protodial --compression=g"
        words = [
            "protodial",
            "--compression",
            "=",
            "gBAD",
            "--header",
            "later value",
        ]
        active = 3
    shell_words = " ".join(shlex.quote(word) for word in words)
    if shell == "bash":
        program = f"""
source "$1"
COMP_LINE={shlex.quote(line)}
COMP_POINT={len(left)}
COMP_WORDS=({shell_words})
COMP_CWORD={active}
_protodial_complete
printf 'VALUE=<%s>\n' "${{COMPREPLY[0]}}"
"""
    else:
        program = f"""
source "$1"
path=("$2" $path)
rehash
compadd() {{ for value in "$@"; do print -r -- "ARG=<$value>"; done; }}
words=({shell_words})
CURRENT={active + 1}
PREFIX='--compression=g'
SUFFIX='BAD'
_protodial
"""
    completed = subprocess.run(
        [
            shell_executable,
            "-c",
            program,
            "cursor-test",
            str(script),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "PROTODIAL_CURSOR_LOG": str(log),
        },
        check=True,
    )
    if shell == "bash":
        assert "gzip" in completed.stdout
    else:
        assert "ARG=<--compression=gzip>" in completed.stdout
    supplied = log.read_bytes().split(b"\0")[:-1]
    assert b"--compression=g" in supplied
    assert supplied[-2:] == [b"--header", b"later value"]
