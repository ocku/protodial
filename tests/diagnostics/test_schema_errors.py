from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial import cli
from protodial.schema.reflection import reflection_database


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"",
        descriptor_pb2.FileDescriptorProto(
            name="invalid.proto", syntax="invalid"
        ).SerializeToString(),
    ],
)
def test_invalid_reflection_descriptor_is_a_sanitized_command_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    diagnostic_format: str,
    payload: bytes,
) -> None:
    class FakeRpcError(Exception):
        pass

    class StatusCode:
        UNIMPLEMENTED = SimpleNamespace(value=(12, "unimplemented"))

    grpc_module = SimpleNamespace(
        RpcError=FakeRpcError,
        StatusCode=StatusCode,
    )

    class Call:
        def __init__(self, response: object) -> None:
            self.response = response
            self.used = False

        def __iter__(self) -> Call:
            return self

        def __next__(self) -> object:
            if self.used:
                raise StopIteration
            self.used = True
            return self.response

        def cancel(self) -> None:
            pass

    class Channel:
        def stream_stream(
            self,
            _path: str,
            *,
            response_deserializer: object,
            **_kwargs: object,
        ) -> object:
            deserialize = response_deserializer
            assert callable(deserialize)

            def rpc(requests: object, **_call_kwargs: object) -> Call:
                request = next(iter(requests))  # type: ignore[arg-type]
                response = reflection_pb2.ServerReflectionResponse()
                response.original_request.ParseFromString(
                    request.SerializeToString()
                )
                response.file_descriptor_response.file_descriptor_proto.append(
                    payload
                )
                return Call(deserialize(response.SerializeToString()))

            return rpc

    def fail(_command: object, _grpc: object) -> None:
        database = reflection_database(Channel(), [], 1.0, grpc_module)
        database.query(file_by_filename="corrupt.proto")

    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda: SimpleNamespace(grpc=grpc_module, protobuf_errors=()),
    )
    monkeypatch.setattr(cli, "invoke", fail)

    assert (
        cli.main(
            [
                "--diagnostic-format",
                diagnostic_format,
                "grpc://localhost:50051/example.API/Call",
            ]
        )
        == 1
    )
    rendered = capsys.readouterr().err
    assert "reflection protocol error: invalid file descriptor payload" in (
        rendered
    )
    assert "DecodeError" not in rendered
    assert "Traceback" not in rendered
    assert "E9001" not in rendered
    if diagnostic_format == "json":
        value = json.loads(rendered)
        assert (value["event"], value["code"]) == (
            "command.failed",
            "E2001",
        )
    else:
        assert rendered.startswith("protodial: (E2001)")


def test_successful_proto_warnings_are_centralized_without_losing_context(
    tmp_path, capfd
) -> None:
    proto = tmp_path / "warning.proto"
    proto.write_text("package example; message Request {}\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["event"] == "schema.compiler_warning"
    assert value["severity"] == "warning"
    assert value["code"] == "W2001"
    assert value["message"] == "proto source does not declare its syntax"
    assert "syntax" in value["causes"][0].lower()
    assert value["spans"][0]["source"] == str(proto)
    assert value["spans"][0]["line"] == 1
    assert value["spans"][0]["text"] == proto.read_text().rstrip("\n")
    assert "proto3" in value["help"][0]


def test_missing_grpcio_tools_is_actionable_without_starting_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd
) -> None:
    import protodial.schema.local as local_schema

    proto = tmp_path / "schema.proto"
    proto.write_text('syntax = "proto3"; package example;\n')
    original_find_spec = local_schema.importlib.util.find_spec

    def find_spec(name: str):
        if name == "grpc_tools.protoc":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(local_schema.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(
        local_schema.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("protoc subprocess was started"),
    )

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "dependency.unavailable"
    assert value["code"] == "E8001"
    assert "grpcio-tools" in value["message"]
    assert "pip install grpcio-tools" in value["message"]
    assert "ModuleNotFoundError" not in captured.err
    assert "Traceback" not in captured.err


def test_malformed_protoset_is_a_located_schema_input_error(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    protoset = tmp_path / "broken.pb"
    protoset.write_bytes(b"\xff\xff\xff")

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--protoset",
            str(protoset),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    value = json.loads(captured.err)
    assert value["event"] == "schema.input_invalid"
    assert value["code"] == "E2002"
    assert value["spans"] == [
        {"source": str(protoset), "label": "invalid schema input"}
    ]
    assert "descriptor_set_out" in value["help"][0]


def test_semantically_invalid_protoset_is_a_located_schema_input_error(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    protoset = tmp_path / "invalid.pb"
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="invalid.proto",
        package="invalid",
        syntax="proto3",
        dependency=["missing.proto"],
    )
    protoset.write_bytes(
        descriptor_pb2.FileDescriptorSet(file=[file_proto]).SerializeToString()
    )

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--protoset",
            str(protoset),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "schema.input_invalid"
    assert value["code"] == "E2002"
    assert value["spans"] == [
        {"source": str(protoset), "label": "invalid schema input"}
    ]
    assert "missing.proto" in value["causes"][0]
