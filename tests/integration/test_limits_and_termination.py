from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from protodial.cli import main

from .support import SERVICE_NAME, RunningServer, Schema, _json_output


@pytest.mark.parametrize(
    ("limit_option", "data"),
    [
        ("--max-recv-message-size", '{"text":"large"}'),
        (
            "--max-send-message-size",
            '{"data":"eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA=="}',
        ),
    ],
)
def test_cli_enforces_grpc_message_size_limits(
    schema: Schema,
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
    limit_option: str,
    data: str,
) -> None:
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            limit_option,
            "20",
            "--data",
            data,
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 1, stderr
    assert output == []
    assert "RESOURCE_EXHAUSTED" in stderr


def test_cli_enforces_rpc_deadline(
    schema: Schema,
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--max-time",
            "0.05",
            "--data",
            "{}",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Slow",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 1
    assert output == []
    assert "DEADLINE_EXCEEDED" in stderr


def test_installed_cli_exits_when_server_terminates_stream_with_stdin_open(
    schema: Schema,
    plaintext_server: RunningServer,
) -> None:
    executable_name = "protodial.exe" if os.name == "nt" else "protodial"
    discovered = shutil.which("protodial")
    executable = (
        Path(discovered)
        if discovered is not None
        else Path(sys.executable).with_name(executable_name)
    )
    assert executable.is_file()
    process = subprocess.Popen(
        [
            str(executable),
            "--proto",
            str(schema.proto),
            "--data",
            "@-",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/TerminalClientStream",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Keep this eligible for posix_spawn: forking while the in-process
        # gRPC test server has active threads is unsafe on macOS.
        close_fds=False,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(b'{"text":"one"}\n')
    process.stdin.flush()

    timed_out = False
    try:
        exit_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        exit_code = process.wait(timeout=5)
    finally:
        process.stdin.close()

    stdout = process.stdout.read()
    stderr = process.stderr.read()
    assert not timed_out, (
        "installed CLI waited for stdin after the RPC terminated"
    )
    assert exit_code == 1
    assert stdout == b""
    assert b"INVALID_ARGUMENT" in stderr
