from __future__ import annotations

import socket

import grpc
import pytest

from protodial.cli import main

from .server import _make_server
from .support import SERVICE_NAME, Certificates, Schema, _json_output


@pytest.mark.parametrize(
    "require_client_certificate", [False, True], ids=("tls", "mtls")
)
def test_cli_verified_tls_and_mtls(
    schema: Schema,
    certificates: Certificates,
    capsys: pytest.CaptureFixture[str],
    require_client_certificate: bool,
) -> None:
    ca = certificates.ca.read_bytes()
    credentials = grpc.ssl_server_credentials(
        (
            (
                certificates.server_key.read_bytes(),
                certificates.server_cert.read_bytes(),
            ),
        ),
        root_certificates=ca if require_client_certificate else None,
        require_client_auth=require_client_certificate,
    )
    running = _make_server(schema, credentials=credentials)
    try:
        arguments = [
            "--proto",
            str(schema.proto),
            "--cacert",
            str(certificates.ca),
            "--data",
            '{"text":"secure"}',
        ]
        if require_client_certificate:
            arguments.extend(
                [
                    "--cert",
                    str(certificates.client_cert),
                    "--key",
                    str(certificates.client_key),
                ]
            )
        arguments.append(f"grpcs://{running.target}/{SERVICE_NAME}/Unary")

        exit_code = main(arguments)
    finally:
        running.server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{"text": "SECURE"}]
    assert stderr == ""


@pytest.mark.parametrize("use_tcp", [False, True])
def test_cli_insecure_tls_tunnel(
    schema: Schema,
    certificates: Certificates,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    use_tcp: bool,
) -> None:
    if use_tcp:
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    credentials = grpc.ssl_server_credentials(
        (
            (
                certificates.server_key.read_bytes(),
                certificates.server_cert.read_bytes(),
            ),
        )
    )
    running = _make_server(schema, credentials=credentials)
    try:
        exit_code = main(
            [
                "--proto",
                str(schema.proto),
                "--insecure",
                "--data",
                '{"text":"tunnel"}',
                f"grpcs://{running.target}/{SERVICE_NAME}/Unary",
            ]
        )
    finally:
        running.server.stop(None).wait()

    output, stderr = _json_output(capsys)
    assert exit_code == 0, stderr
    assert output == [{"text": "TUNNEL"}]
    assert (
        "* warning (W3001): TLS certificate verification is disabled" in stderr
    )
