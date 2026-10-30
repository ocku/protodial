from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent import futures
from pathlib import Path
from threading import Timer

import grpc
import pytest

from protodial.cli import main

from .server import _handlers
from .support import SERVICE_NAME, RunningServer, Schema, _json_output


@pytest.mark.parametrize(
    ("option", "value", "argument"),
    [
        ("--data", "@{path}", "--data"),
        ("--field:file", "data={path}", "--field:file"),
    ],
)
def test_cli_reports_missing_request_files_as_structured_local_io(
    schema: Schema,
    plaintext_server: RunningServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    value: str,
    argument: str,
) -> None:
    missing = tmp_path / "missing-request.bin"
    option_value = value.format(path=missing)
    exit_code = main(
        [
            "--diagnostic-format",
            "json",
            "--proto",
            str(schema.proto),
            option,
            option_value,
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    start = option_value.rfind(str(missing))
    assert exit_code == 1
    assert captured.out == ""
    assert diagnostic["event"] == "local_io.failed"
    assert diagnostic["code"] == "E8002"
    assert diagnostic["context"]["path"] == str(missing)
    assert diagnostic["context"]["argument"] == argument
    assert diagnostic["causes"] == ["No such file or directory"]
    assert diagnostic["spans"] == [
        {
            "source": "argument 6",
            "text": option_value,
            "start": start,
            "end": start + len(str(missing)),
            "label": "path could not be read",
        }
    ]


@pytest.mark.parametrize("input_format", ["raw", "protobuf"])
def test_cli_reads_delayed_unary_pipe_to_eof(
    schema: Schema,
    plaintext_server: RunningServer,
    input_format: str,
) -> None:
    request = (
        b"delayed pipe"
        if input_format == "raw"
        else schema.payload(text="delayed pipe").SerializeToString()
    )
    format_options = (
        ["--input-field", "data", "--output-field", "data"]
        if input_format == "raw"
        else []
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "protodial",
            "--proto",
            str(schema.proto),
            "--input-format",
            input_format,
            "--output-format",
            input_format,
            *format_options,
            "--data",
            "@-",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        process.stdin.write(request[:1])
        process.stdin.flush()
        time.sleep(0.1)
        process.stdin.write(request[1:])
        process.stdin.close()
        exit_code = process.wait(timeout=5)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise

    stdout = process.stdout.read()
    stderr = process.stderr.read()
    expected = (
        request
        if input_format == "raw"
        else schema.payload(text="DELAYED PIPE").SerializeToString()
    )
    assert exit_code == 0, stderr.decode(errors="replace")
    assert stdout == expected
    assert b"error:" not in stderr


def test_cli_uses_a_protoset_without_reflection(
    schema: Schema,
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--protoset",
            str(schema.protoset),
            "--data",
            '{"text":"set"}',
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "SET"}]
    assert stderr == ""
    assert all(
        "ServerReflection" not in method
        for method, _metadata in plaintext_server.metadata
    )


def test_wait_for_ready_defers_local_rpc_connection_until_rpc_deadline(
    schema: Schema,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(SERVICE_NAME, _handlers(schema)),)
    )
    port = server.add_insecure_port("localhost:0")
    assert port
    target = f"grpc://localhost:{port}/{SERVICE_NAME}/Unary"
    common = [
        "--proto",
        str(schema.proto),
        "--connect-timeout",
        "0.1",
        "--max-time",
        "3",
        "--data",
        '{"text":"ready"}',
    ]

    assert main([*common, target]) == 1
    assert "connection was not ready" in capsys.readouterr().err

    start = Timer(0.25, server.start)
    start.start()
    try:
        assert main([*common, "--wait-for-ready", target]) == 0
        output, stderr = _json_output(capsys)
        assert output == [{"text": "READY"}]
        assert stderr == ""
    finally:
        start.join()
        server.stop(None).wait()
