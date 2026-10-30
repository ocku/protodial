from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from protodial.cli import main

from .server import _handlers
from .support import SERVICE_NAME, RunningServer, Schema, _json_output


@pytest.mark.parametrize("target_form", ["dns-triple", "dns-single", "ipv4"])
def test_cli_invokes_through_native_resolver_target(
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
    target_form: str,
) -> None:
    _host, port = plaintext_server.target.rsplit(":", 1)
    if target_form == "dns-triple":
        target = f"dns:///{plaintext_server.target}"
    elif target_form == "dns-single":
        target = f"dns:/{plaintext_server.target}"
    else:
        # Repeating the address exercises grpcio's comma-delimited resolver
        # syntax without requiring a second server.
        endpoint = f"127.0.0.1:{port}"
        target = f"ipv4:{endpoint},{endpoint}"
    exit_code = main(
        [
            "--plaintext",
            "--data",
            f'{{"text":"{target_form}"}}',
            "--method",
            f"{SERVICE_NAME}/Unary",
            target,
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": target_form.upper()}]
    assert stderr == ""


@pytest.mark.parametrize("target_prefix", ["unix:", "unix://"])
def test_cli_invokes_through_unix_socket_target(
    schema: Schema,
    capsys: pytest.CaptureFixture[str],
    target_prefix: str,
) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix-domain sockets are unavailable")

    with tempfile.TemporaryDirectory(prefix="g-") as directory:
        socket_path = Path(directory) / "grpc.sock"
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    SERVICE_NAME, _handlers(schema)
                ),
            )
        )
        socket_target = f"unix:{socket_path.as_posix()}"
        try:
            bound = server.add_insecure_port(socket_target)
        except RuntimeError:
            bound = 0
        if not bound:
            pytest.skip("grpcio cannot bind a Unix-domain socket")
        server.start()
        try:
            exit_code = main(
                [
                    "--plaintext",
                    "--proto",
                    str(schema.proto),
                    "--data",
                    '{"text":"unix"}',
                    "--method",
                    f"{SERVICE_NAME}/Unary",
                    f"{target_prefix}{socket_path}",
                ]
            )
        finally:
            server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "UNIX"}]
    assert stderr == ""


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="abstract Unix sockets are a Linux transport",
)
def test_cli_invokes_through_abstract_unix_socket_target(
    schema: Schema,
    capsys: pytest.CaptureFixture[str],
) -> None:
    socket_name = f"protodial-{os.getpid()}-{time.time_ns()}"
    target = f"unix-abstract:{socket_name}"
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(SERVICE_NAME, _handlers(schema)),)
    )
    try:
        bound = server.add_insecure_port(target)
    except RuntimeError:
        bound = 0
    if not bound:
        pytest.skip("grpcio cannot bind an abstract Unix-domain socket")
    server.start()
    try:
        exit_code = main(
            [
                "--plaintext",
                "--proto",
                str(schema.proto),
                "--data",
                '{"text":"abstract"}',
                "--method",
                f"{SERVICE_NAME}/Unary",
                target,
            ]
        )
    finally:
        server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "ABSTRACT"}]
    assert stderr == ""
