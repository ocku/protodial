from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from protodial.cli import main

from .support import (
    SERVICE_NAME,
    VALIDATOR_SERVICE_NAME,
    RunningServer,
    Schema,
    ValidatorSchema,
    _json_output,
)


def test_cli_renders_rich_status_from_a_real_server(
    schema: Schema,
    plaintext_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--format-error",
            "--data",
            "{}",
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/RichError",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 1
    assert output == []
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in stderr
    assert "  reason: invalid payload" in stderr
    assert "Payload.text: must be useful" in stderr
    assert "  call: `e2e.Harness/RichError`" in stderr
    assert plaintext_server.target not in stderr
    assert stderr.index("Payload.text: must be useful") < stderr.index(
        "protodial: (E4103)"
    )


@pytest.mark.parametrize(
    "method_alias", ["Validator/Validate", "errors.Validator/Validate"]
)
def test_cli_failure_uses_canonical_method_for_suffix_aliases(
    validator_schema: ValidatorSchema,
    validator_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
    method_alias: str,
) -> None:
    exit_code = main(
        [
            "--proto",
            str(validator_schema.proto),
            "--format-error",
            "--data",
            "{}",
            f"grpc://{validator_server.target}/{method_alias}",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert f"  call: `{VALIDATOR_SERVICE_NAME}/Validate`" in captured.err
    assert validator_server.target not in captured.err


@pytest.mark.parametrize(
    "method_alias", ["Validator/Validate", "errors.Validator/Validate"]
)
def test_cli_ndjson_propagates_canonical_method_for_suffix_aliases(
    validator_schema: ValidatorSchema,
    validator_server: RunningServer,
    capsys: pytest.CaptureFixture[str],
    method_alias: str,
) -> None:
    exit_code = main(
        [
            "--diagnostic-format",
            "json",
            "--verbose",
            "--verbose",
            "--proto",
            str(validator_schema.proto),
            "--format-error",
            "--data",
            "{}",
            f"grpc://{validator_server.target}/{method_alias}",
        ]
    )

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines()]
    by_name = {event["event"]: event for event in events}
    canonical = f"{VALIDATOR_SERVICE_NAME}/Validate"
    assert exit_code == 1
    assert captured.out == ""
    assert by_name["rpc.resolved"]["context"]["method"] == canonical
    assert by_name["rpc.started"]["context"]["method"] == canonical
    assert by_name["rpc.failed"]["context"]["method"] == canonical
    assert by_name["rpc.failed"]["context"]["target"] == validator_server.target
    assert by_name["rpc.failed"]["message"] == (
        f"RPC `{canonical}` failed with INVALID_ARGUMENT (3)"
    )


def test_cli_exports_descriptors_after_a_successful_call(
    schema: Schema,
    plaintext_server: RunningServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "exported.pb"
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--protoset-out",
            str(output_path),
            "--data",
            '{"text":"export"}',
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    output, stderr = _json_output(capsys)
    exported = descriptor_pb2.FileDescriptorSet.FromString(
        output_path.read_bytes()
    )
    assert exit_code == 0
    assert output == [{"text": "EXPORT"}]
    assert stderr == ""
    assert [file.name for file in exported.file] == ["harness.proto"]


def test_cli_reports_descriptor_export_failure_after_a_successful_call(
    schema: Schema,
    plaintext_server: RunningServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "exported.pb"
    output_path.mkdir()
    exit_code = main(
        [
            "--proto",
            str(schema.proto),
            "--protoset-out",
            str(output_path),
            "--data",
            '{"text":"export"}',
            f"grpc://{plaintext_server.target}/{SERVICE_NAME}/Unary",
        ]
    )

    output, stderr = _json_output(capsys)
    assert exit_code == 1
    assert output == [{"text": "EXPORT"}]
    assert "protodial: (E8002)" in stderr
    assert "exported.pb" in stderr
    assert output_path.is_dir()
