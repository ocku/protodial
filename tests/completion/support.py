from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from google.protobuf import descriptor_pb2

from protodial.commands.models import Command
from protodial.completion.engine import Candidate
from protodial.schema.local import local_descriptor_source

try:
    import pty
except ImportError:  # pragma: no cover - Windows has no Unix PTYs
    pty = None  # type: ignore[assignment]


PROTO = """\
syntax = "proto3";
package saved.example;

service Greeter {
  rpc SayHello(Request) returns (Reply);
}

message Request {
  enum Mood {
    MOOD_UNSPECIFIED = 0;
    MOOD_HAPPY = 1;
  }
  string display_name = 1;
  bytes payload = 2;
  Mood mood = 3;
  bool enabled = 4;
  repeated Mood moods = 5;
  repeated bool flags = 6;
}

message Reply {
  bytes body = 1;
}
"""


def _values(candidates: list[Candidate]) -> list[str]:
    return [candidate.value for candidate in candidates]


def _environment(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}


def _mkfifo_or_skip(path: Path) -> None:
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:
        pytest.skip("FIFOs are unavailable on this platform")
    mkfifo(path)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")


def _saved_proto(tmp_path: Path) -> tuple[dict[str, str], Path]:
    environment = _environment(tmp_path)
    path = tmp_path / "data" / "protodial" / "protos" / "api.proto"
    path.parent.mkdir(parents=True)
    path.write_text(PROTO, encoding="utf-8")
    return environment, path


def _collision_proto(field: str, enum_value: str) -> str:
    return (
        'syntax = "proto3";\n'
        "package collision;\n"
        "service API { rpc Call(Request) returns (Reply); }\n"
        "message Request {\n"
        "  enum Mood { MOOD_UNSPECIFIED = 0; "
        f"{enum_value} = 1; }}\n"
        f"  string {field} = 1;\n"
        "  Mood mood = 2;\n"
        "}\n"
        f"message Reply {{ bytes {field}_reply = 1; }}\n"
    )


def _compiled_source(tmp_path: Path, name: str, proto: str) -> object:
    path = tmp_path / name
    path.write_text(proto, encoding="utf-8")
    command = cast(
        Command,
        cast(
            object,
            SimpleNamespace(
                schema=SimpleNamespace(
                    proto=(str(path),),
                    proto_path=(),
                    protoset=(),
                )
            ),
        ),
    )
    return local_descriptor_source(command)


def _write_protoset(tmp_path: Path, name: str = "schema.protoset") -> Path:
    source = _compiled_source(tmp_path, "protoset-source.proto", PROTO)
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    for file_proto in source.file_protos.values():  # type: ignore[attr-defined]
        descriptor_set.file.add().CopyFrom(file_proto)
    path = tmp_path / name
    path.write_bytes(descriptor_set.SerializeToString())
    return path


def _fake_protodial(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "protodial"
    log = tmp_path / "arguments.log"
    input_log = tmp_path / "input.log"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$PROTODIAL_TEST_LOG"\n'
        'cat > "$PROTODIAL_TEST_INPUT"\n'
        'if [ "$PROTODIAL_TEST_MODE" = array ]; then\n'
        "  printf 'moods=[\"MOOD_HAPPY\"]\\000enum array\\0001\\000'\n"
        'elif [ "$PROTODIAL_TEST_MODE" = value ]; then\n'
        "  printf 'mood=MOOD_HAPPY\\000enum\\0001\\000'\n"
        "else\n"
        "  printf 'mood=\\000enum\\0000\\000'\n"
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log, input_log


def _read_pty(file_descriptor: int, duration: float = 0.2) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        readable, _, _ = select.select([file_descriptor], [], [], 0.03)
        if not readable:
            continue
        try:
            output.extend(os.read(file_descriptor, 65536))
        except OSError:
            break
    return bytes(output)


def _completion_script(tmp_path: Path, shell: str) -> Path:
    name = "protodial.bash" if shell == "bash" else "_protodial"
    script = tmp_path / name
    script.write_bytes(
        files("protodial.completion").joinpath(name).read_bytes()
    )
    return script


def _readline_completion(
    tmp_path: Path, line: str, candidate: str
) -> list[str]:
    if pty is None or not Path("/bin/bash").is_file():
        pytest.skip("interactive Bash completion requires a Unix PTY")
    executable = tmp_path / "protodial"
    log = tmp_path / "readline-arguments.json"
    ready = tmp_path / "readline-completion-ready"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "if sys.argv[1:2] == ['__complete']:\n"
        "    sys.stdin.buffer.read()\n"
        "    value = os.environ['PROTODIAL_PTY_VALUE'].encode()\n"
        "    sys.stdout.buffer.write(value + b'\\0value\\0' + b'1\\0')\n"
        "    sys.stdout.buffer.flush()\n"
        "    pathlib.Path(os.environ['PROTODIAL_PTY_READY']).touch()\n"
        "else:\n"
        "    pathlib.Path(os.environ['PROTODIAL_PTY_LOG']).write_text(\n"
        "        json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    script = _completion_script(tmp_path, "bash")
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROTODIAL_PTY_LOG": str(log),
        "PROTODIAL_PTY_VALUE": candidate,
        "PROTODIAL_PTY_READY": str(ready),
    }
    file_descriptor, slave = pty.openpty()
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    try:
        _read_pty(file_descriptor, 0.5)
        setup = f"PS1='READY> '; source {shlex.quote(str(script))}\n"
        os.write(file_descriptor, setup.encode())
        prompt_output = bytearray()
        prompt_deadline = time.monotonic() + 5.0
        while (
            b"READY> " not in prompt_output
            and time.monotonic() < prompt_deadline
        ):
            prompt_output.extend(_read_pty(file_descriptor, 0.05))
        assert b"READY> " in prompt_output
        os.write(file_descriptor, line.encode() + b"\t")
        completion_deadline = time.monotonic() + 5.0
        while not ready.exists() and time.monotonic() < completion_deadline:
            _read_pty(file_descriptor, 0.05)
        assert ready.exists()
        _read_pty(file_descriptor, 0.3)
        os.write(file_descriptor, b"\n")
        deadline = time.monotonic() + 3.0
        while not log.exists() and time.monotonic() < deadline:
            _read_pty(file_descriptor, 0.05)
        assert log.exists(), _read_pty(file_descriptor, 0.2).decode(
            errors="replace"
        )
        return json.loads(log.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    finally:
        try:
            os.write(file_descriptor, b"\x03exit\n")
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
        os.close(file_descriptor)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
