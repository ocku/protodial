from __future__ import annotations

from pathlib import Path

import pytest

from protodial.cli import main
from protodial.presentation.descriptor import render_descriptor

from .support import SERVICE_NAME, RunningServer, Schema, _json_output


def test_method_descriptor_can_be_rendered(schema: Schema) -> None:
    method = schema.pool.FindMethodByName("e2e.Harness.Unary")

    assert (
        render_descriptor(method)
        == "rpc Unary (.e2e.Payload) returns (.e2e.Payload);"
    )


@pytest.mark.parametrize(
    ("method", "data", "expected"),
    [
        ("Unary", '{"text":"one"}', [{"text": "ONE"}]),
        (
            "ServerStream",
            '{"text":"one"}',
            [{"text": "one-1"}, {"text": "one-2"}],
        ),
        (
            "ClientStream",
            '{"text":"one"} {"text":"two"}',
            [{"text": "one|two"}],
        ),
        (
            "Bidi",
            '{"text":"one"} {"text":"two"}',
            [{"text": "ONE"}, {"text": "TWO"}],
        ),
    ],
)
def test_cli_invokes_all_rpc_cardinalities_with_a_local_proto(
    schema: Schema,
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
    method: str,
    data: str,
    expected: list[dict[str, object]],
) -> None:
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--data",
            data,
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/{method}",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == expected
    assert stderr == ""


@pytest.mark.parametrize(
    "method",
    ["Unary", "ClientStream", "UnaryTextOnly", "ClientStreamTextOnly"],
)
def test_omitted_raw_input_is_independent_of_request_fields(
    schema: Schema,
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
    method: str,
) -> None:
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--input-format",
            "raw",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/{method}",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 0
    assert output == [{}]
    assert stderr == ""


def test_cli_round_trips_text_format(
    schema: Schema,
    plaintext_server: RunningServer,
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--input-format",
            "text",
            "--output-format",
            "text",
            "--data",
            'text: "text"',
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    captured = capfdbinary.readouterr()
    assert exit_code == 0
    assert captured.out == b'text: "TEXT"\n'
    assert captured.err == b""


def test_cli_round_trips_serialized_protobuf(
    schema: Schema,
    plaintext_server: RunningServer,
    tmp_path: Path,
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    request = tmp_path / "request.pb"
    request.write_bytes(schema.payload(text="protobuf").SerializeToString())

    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--input-format",
            "protobuf",
            "--output-format",
            "protobuf",
            "--data",
            f"@{request}",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    captured = capfdbinary.readouterr()
    assert exit_code == 0
    assert schema.payload.FromString(captured.out) == schema.payload(
        text="PROTOBUF"
    )
    assert captured.err == b""


def test_cli_round_trips_raw_bytes(
    schema: Schema,
    plaintext_server: RunningServer,
    tmp_path: Path,
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    request = tmp_path / "request.bin"
    request.write_bytes(b"\x00raw\xff")

    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--input-format",
            "raw",
            "--input-field",
            "data",
            "--output-format",
            "raw",
            "--output-field",
            "data",
            "--data",
            f"@{request}",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    captured = capfdbinary.readouterr()
    assert exit_code == 0
    assert captured.out == b"\x00raw\xff"
    assert captured.err == b""
