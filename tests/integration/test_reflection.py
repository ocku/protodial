from __future__ import annotations

import pytest

from protodial.cli import main

from .server import _make_server
from .support import SERVICE_NAME, RunningServer, Schema, _json_output


def test_cli_reflection_metadata_compression_and_diagnostics(
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--compression",
            "gzip",
            "--header",
            "shared: common",
            "--reflect-header",
            "reflection-only: schema",
            "--rpc-header",
            "rpc-only: call",
            "--verbose",
            "--verbose",
            "--data",
            '{"text":"reflected"}',
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "REFLECTED"}]
    assert "e2e-header: ready" in stderr
    assert "e2e-trailer: done" in stderr
    assert "* OK" in stderr
    assert "server reflection negotiated v1alpha" in stderr
    assert stderr.count("sent 1 message; received 1 message") == 1
    assert stderr.index("server reflection negotiated v1alpha") < stderr.index(
        "using reflection schema"
    )
    assert stderr.index("using reflection schema") < stderr.index("resolved")

    reflection_metadata = [
        dict(metadata)
        for method, metadata in plaintext_server.metadata
        if "ServerReflection" in method
    ]
    rpc_metadata = [
        dict(metadata)
        for method, metadata in plaintext_server.metadata
        if method == f"/{SERVICE_NAME}/Unary"
    ]
    assert reflection_metadata
    assert rpc_metadata
    assert all(values["shared"] == "common" for values in reflection_metadata)
    assert all(
        values["reflection-only"] == "schema" for values in reflection_metadata
    )
    assert all("rpc-only" not in values for values in reflection_metadata)
    assert rpc_metadata[-1]["shared"] == "common"
    assert rpc_metadata[-1]["rpc-only"] == "call"
    assert "reflection-only" not in rpc_metadata[-1]


def test_cli_uses_stable_v1_reflection(
    schema: Schema,
    capsys: pytest.CaptureFixture[str],
) -> None:
    running = _make_server(schema, stable_reflection=True)
    try:
        exit_code = main(
            [
                "--verbose",
                "--verbose",
                "--data",
                '{"text":"stable"}',
                f"grpc://{running.target}/{SERVICE_NAME}/Unary",
            ]
        )
    finally:
        running.server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "STABLE"}]
    assert "server reflection negotiated v1" in stderr
    assert any(
        method.startswith("/grpc.reflection.v1.ServerReflection/")
        for method, _metadata in running.metadata
    )


def test_cli_sanitizes_reflection_protocol_errors(
    schema: Schema,
    capsys: pytest.CaptureFixture[str],
) -> None:
    running = _make_server(
        schema,
        stable_reflection=True,
        reflection_error="bad\nforged\rline\x1b[2J\u202eowned",
    )
    try:
        exit_code = main(["list", "--plaintext", running.target])
    finally:
        running.server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 1
    assert output == []
    assert "reflection: 13: bad\\nforged\\rline\\x1b[2J\\u202eowned" in stderr
    assert "\x1b" not in stderr


def test_dns_resolver_sends_endpoint_as_reflection_host(
    schema: Schema,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_hosts: list[str] = []
    running = _make_server(
        schema,
        stable_reflection=True,
        expected_reflection_hosts=expected_hosts,
    )
    expected_hosts.append(running.target)
    try:
        exit_code = main(
            [
                "--plaintext",
                "--data",
                '{"text":"authority"}',
                "--method",
                f"{SERVICE_NAME}/Unary",
                f"dns:///{running.target}",
            ]
        )
    finally:
        running.server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "AUTHORITY"}]
    assert stderr == ""
