"""End-to-end tests from CLI argument parsing through a real gRPC server."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent import futures
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Timer
from typing import Any

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.rpc import error_details_pb2, status_pb2
from grpc_reflection.v1alpha import reflection, reflection_pb2
from grpc_tools import protoc

from protodial.cli import main
from protodial.presentation.descriptor import render_descriptor

SERVICE_NAME = "e2e.Harness"
PROTO = """\
syntax = "proto3";
package e2e;

message Payload {
  string text = 1;
  bytes data = 2;
}

message TextOnly {
  string text = 1;
}

service Harness {
  rpc Unary(Payload) returns (Payload);
  rpc UnaryTextOnly(TextOnly) returns (Payload);
  rpc ServerStream(Payload) returns (stream Payload);
  rpc ClientStream(stream Payload) returns (Payload);
  rpc ClientStreamTextOnly(stream TextOnly) returns (Payload);
  rpc Bidi(stream Payload) returns (stream Payload);
  rpc TerminalClientStream(stream Payload) returns (Payload);
  rpc RichError(Payload) returns (Payload);
  rpc Slow(Payload) returns (Payload);
}
"""

VALIDATOR_SERVICE_NAME = "examples.errors.Validator"
VALIDATOR_PROTO = """\
syntax = "proto3";
package examples.errors;

message ValidateRequest { string resource = 1; }
message ValidateReply { string resource = 1; bool valid = 2; }

service Validator {
  rpc Validate(ValidateRequest) returns (ValidateReply);
}
"""


@dataclass(frozen=True)
class Schema:
    proto: Path
    protoset: Path
    pool: descriptor_pool.DescriptorPool
    payload: type[Any]
    text_only: type[Any]


@dataclass
class RunningServer:
    target: str
    metadata: list[tuple[str, tuple[tuple[str, str | bytes], ...]]]
    server: grpc.Server


@dataclass(frozen=True)
class Certificates:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path


@dataclass(frozen=True)
class ValidatorSchema:
    proto: Path
    pool: descriptor_pool.DescriptorPool
    request: type[Any]
    reply: type[Any]


@pytest.fixture(autouse=True)
def _isolated_default_proto_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_home = tmp_path / "data"
    (data_home / "protodial" / "protos").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> Schema:
    root = tmp_path_factory.mktemp("cli-e2e-schema")
    proto = root / "harness.proto"
    protoset = root / "harness.pb"
    proto.write_text(PROTO, encoding="utf-8")
    status = protoc.main(
        [
            "protodial-test",
            f"-I{root}",
            f"--descriptor_set_out={protoset}",
            "--include_imports",
            proto.name,
        ]
    )
    assert status == 0

    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        protoset.read_bytes()
    )
    pool = descriptor_pool.DescriptorPool()
    for file_proto in descriptor_set.file:
        pool.Add(file_proto)
    payload = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("e2e.Payload")
    )
    text_only = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("e2e.TextOnly")
    )
    return Schema(proto, protoset, pool, payload, text_only)


@pytest.fixture(scope="module")
def validator_schema(
    tmp_path_factory: pytest.TempPathFactory,
) -> ValidatorSchema:
    root = tmp_path_factory.mktemp("cli-validator-schema")
    proto = root / "errors.proto"
    protoset = root / "errors.pb"
    proto.write_text(VALIDATOR_PROTO, encoding="utf-8")
    status = protoc.main(
        [
            "protodial-test",
            f"-I{root}",
            f"--descriptor_set_out={protoset}",
            "--include_imports",
            proto.name,
        ]
    )
    assert status == 0

    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        protoset.read_bytes()
    )
    pool = descriptor_pool.DescriptorPool()
    for file_proto in descriptor_set.file:
        pool.Add(file_proto)
    request = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("examples.errors.ValidateRequest")
    )
    reply = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("examples.errors.ValidateReply")
    )
    return ValidatorSchema(proto, pool, request, reply)


class MetadataRecorder(grpc.ServerInterceptor):
    def __init__(
        self, records: list[tuple[str, tuple[tuple[str, str | bytes], ...]]]
    ) -> None:
        self._records = records
        self._lock = Lock()

    def intercept_service(
        self,
        continuation: Any,
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        metadata = tuple(
            (item.key, item.value)
            for item in handler_call_details.invocation_metadata
        )
        with self._lock:
            self._records.append((handler_call_details.method, metadata))
        return continuation(handler_call_details)


def _handlers(schema: Schema) -> dict[str, grpc.RpcMethodHandler]:
    payload = schema.payload
    text_only = schema.text_only

    def prepare(context: grpc.ServicerContext) -> None:
        context.send_initial_metadata((("e2e-header", "ready"),))
        context.set_trailing_metadata((("e2e-trailer", "done"),))

    def unary(request: Any, context: grpc.ServicerContext) -> Any:
        prepare(context)
        if request.text == "large":
            return payload(text="large", data=b"x" * 1024)
        return payload(text=request.text.upper(), data=request.data)

    def server_stream(
        request: Any, context: grpc.ServicerContext
    ) -> Iterator[Any]:
        prepare(context)
        yield payload(text=f"{request.text}-1")
        yield payload(text=f"{request.text}-2")

    def unary_text_only(request: Any, context: grpc.ServicerContext) -> Any:
        prepare(context)
        return payload(text=request.text.upper())

    def client_stream(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Any:
        prepare(context)
        return payload(text="|".join(request.text for request in requests))

    def client_stream_text_only(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Any:
        prepare(context)
        return payload(text="|".join(request.text for request in requests))

    def bidi(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Iterator[Any]:
        prepare(context)
        for request in requests:
            yield payload(text=request.text.upper())

    def terminal_client_stream(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Any:
        next(requests)
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "stream rejected")

    def rich_error(_request: Any, context: grpc.ServicerContext) -> Any:
        detail = error_details_pb2.BadRequest()
        detail.field_violations.add(field="text", description="must be useful")
        status = status_pb2.Status(
            code=grpc.StatusCode.INVALID_ARGUMENT.value[0],
            message="invalid payload",
        )
        status.details.add().Pack(detail)
        context.set_trailing_metadata(
            (("grpc-status-details-bin", status.SerializeToString()),)
        )
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid payload")

    def slow(request: Any, context: grpc.ServicerContext) -> Any:
        while context.is_active():
            time.sleep(0.005)
        return request

    deserialize = payload.FromString
    serialize = payload.SerializeToString
    return {
        "Unary": grpc.unary_unary_rpc_method_handler(
            unary,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "UnaryTextOnly": grpc.unary_unary_rpc_method_handler(
            unary_text_only,
            request_deserializer=text_only.FromString,
            response_serializer=serialize,
        ),
        "ServerStream": grpc.unary_stream_rpc_method_handler(
            server_stream,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "ClientStream": grpc.stream_unary_rpc_method_handler(
            client_stream,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "ClientStreamTextOnly": grpc.stream_unary_rpc_method_handler(
            client_stream_text_only,
            request_deserializer=text_only.FromString,
            response_serializer=serialize,
        ),
        "Bidi": grpc.stream_stream_rpc_method_handler(
            bidi,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "TerminalClientStream": grpc.stream_unary_rpc_method_handler(
            terminal_client_stream,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "RichError": grpc.unary_unary_rpc_method_handler(
            rich_error,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "Slow": grpc.unary_unary_rpc_method_handler(
            slow,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
    }


def _stable_reflection_handler(
    schema: Schema,
    expected_hosts: list[str] | None = None,
    error_message: str | None = None,
) -> grpc.GenericRpcHandler:
    file_proto = descriptor_pb2.FileDescriptorProto.FromString(
        reflection_pb2.DESCRIPTOR.serialized_pb
    )
    file_proto.name = "grpc/reflection/v1/reflection.proto"
    file_proto.package = "grpc.reflection.v1"

    def update_message_types(message: descriptor_pb2.DescriptorProto) -> None:
        for field in message.field:
            if field.type_name.startswith(".grpc.reflection.v1alpha."):
                field.type_name = field.type_name.replace(
                    ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                )
        for nested in message.nested_type:
            update_message_types(nested)

    for message in file_proto.message_type:
        update_message_types(message)
    for service in file_proto.service:
        for method in service.method:
            method.input_type = method.input_type.replace(
                ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
            )
            method.output_type = method.output_type.replace(
                ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
            )

    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    request_class = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("grpc.reflection.v1.ServerReflectionRequest")
    )
    response_class = message_factory.GetMessageClass(
        pool.FindMessageTypeByName(
            "grpc.reflection.v1.ServerReflectionResponse"
        )
    )
    schema_files = descriptor_pb2.FileDescriptorSet.FromString(
        schema.protoset.read_bytes()
    ).file

    def reflect(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Iterator[Any]:
        for request in requests:
            if expected_hosts is not None and request.host != expected_hosts[0]:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"unexpected reflection host: {request.host!r}",
                )
            response = response_class(valid_host=request.host)
            response.original_request.CopyFrom(request)
            if error_message is not None:
                response.error_response.error_code = (
                    grpc.StatusCode.INTERNAL.value[0]
                )
                response.error_response.error_message = error_message
                yield response
                continue
            request_kind = request.WhichOneof("message_request")
            if request_kind == "list_services":
                response.list_services_response.service.add(name=SERVICE_NAME)
                response.list_services_response.service.add(
                    name="grpc.reflection.v1.ServerReflection"
                )
            elif request_kind in {"file_by_filename", "file_containing_symbol"}:
                response.file_descriptor_response.file_descriptor_proto.extend(
                    file.SerializeToString() for file in schema_files
                )
            elif request_kind == "all_extension_numbers_of_type":
                response.all_extension_numbers_response.base_type_name = (
                    request.all_extension_numbers_of_type
                )
            else:
                response.error_response.error_code = (
                    grpc.StatusCode.NOT_FOUND.value[0]
                )
                response.error_response.error_message = "descriptor not found"
            yield response

    return grpc.method_handlers_generic_handler(
        "grpc.reflection.v1.ServerReflection",
        {
            "ServerReflectionInfo": grpc.stream_stream_rpc_method_handler(
                reflect,
                request_deserializer=request_class.FromString,
                response_serializer=response_class.SerializeToString,
            )
        },
    )


def _make_server(
    schema: Schema,
    *,
    credentials: grpc.ServerCredentials | None = None,
    stable_reflection: bool = False,
    expected_reflection_hosts: list[str] | None = None,
    reflection_error: str | None = None,
) -> RunningServer:
    records: list[tuple[str, tuple[tuple[str, str | bytes], ...]]] = []
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=8),
        interceptors=(MetadataRecorder(records),),
    )
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(SERVICE_NAME, _handlers(schema)),)
    )
    if stable_reflection:
        server.add_generic_rpc_handlers(
            (
                _stable_reflection_handler(
                    schema, expected_reflection_hosts, reflection_error
                ),
            )
        )
    else:
        reflection.enable_server_reflection(
            (SERVICE_NAME, reflection.SERVICE_NAME), server, pool=schema.pool
        )
    address = "localhost:0"
    port = (
        server.add_secure_port(address, credentials)
        if credentials is not None
        else server.add_insecure_port(address)
    )
    assert port
    server.start()
    return RunningServer(f"localhost:{port}", records, server)


@pytest.fixture
def plaintext_server(schema: Schema) -> Iterator[RunningServer]:
    running = _make_server(schema)
    try:
        yield running
    finally:
        running.server.stop(None).wait()


@pytest.fixture
def validator_server(
    validator_schema: ValidatorSchema,
) -> Iterator[RunningServer]:
    records: list[tuple[str, tuple[tuple[str, str | bytes], ...]]] = []
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        interceptors=(MetadataRecorder(records),),
    )

    def validate(_request: Any, context: grpc.ServicerContext) -> Any:
        detail = error_details_pb2.BadRequest()
        detail.field_violations.add(
            field="resource", description="resource must not be empty"
        )
        status = status_pb2.Status(
            code=grpc.StatusCode.INVALID_ARGUMENT.value[0],
            message="validation failed",
        )
        status.details.add().Pack(detail)
        context.set_trailing_metadata(
            (("grpc-status-details-bin", status.SerializeToString()),)
        )
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "validation failed")

    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                VALIDATOR_SERVICE_NAME,
                {
                    "Validate": grpc.unary_unary_rpc_method_handler(
                        validate,
                        request_deserializer=validator_schema.request.FromString,
                        response_serializer=validator_schema.reply.SerializeToString,
                    )
                },
            ),
        )
    )
    reflection.enable_server_reflection(
        (VALIDATOR_SERVICE_NAME, reflection.SERVICE_NAME),
        server,
        pool=validator_schema.pool,
    )
    port = server.add_insecure_port("localhost:0")
    assert port
    server.start()
    running = RunningServer(f"localhost:{port}", records, server)
    try:
        yield running
    finally:
        running.server.stop(None).wait()


def test_method_descriptor_can_be_rendered(schema: Schema) -> None:
    method = schema.pool.FindMethodByName("e2e.Harness.Unary")

    assert (
        render_descriptor(method)
        == "rpc Unary (.e2e.Payload) returns (.e2e.Payload);"
    )


def _json_output(
    capfd: pytest.CaptureFixture[str],
) -> tuple[list[dict[str, object]], str]:
    captured = capfd.readouterr()
    return [
        json.loads(line) for line in captured.out.splitlines()
    ], captured.err


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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
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
    capfd: pytest.CaptureFixture[str],
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

    captured = capfd.readouterr()
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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": "SET"}]
    assert stderr == ""
    assert all(
        "ServerReflection" not in method
        for method, _metadata in plaintext_server.metadata
    )


def test_wait_for_ready_defers_local_rpc_connection_until_rpc_deadline(
    schema: Schema,
    capfd: pytest.CaptureFixture[str],
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
    assert "connection was not ready" in capfd.readouterr().err

    start = Timer(0.25, server.start)
    start.start()
    try:
        assert main([*common, "--wait-for-ready", target]) == 0
        output, stderr = _json_output(capfd)
        assert output == [{"text": "READY"}]
        assert stderr == ""
    finally:
        start.join()
        server.stop(None).wait()


@pytest.mark.parametrize("target_form", ["dns-triple", "dns-single", "ipv4"])
def test_cli_invokes_through_native_resolver_target(
    plaintext_server: RunningServer,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": target_form.upper()}]
    assert stderr == ""


@pytest.mark.parametrize("target_prefix", ["unix:", "unix://"])
def test_cli_invokes_through_unix_socket_target(
    schema: Schema,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": "UNIX"}]
    assert stderr == ""


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="abstract Unix sockets are a Linux transport",
)
def test_cli_invokes_through_abstract_unix_socket_target(
    schema: Schema,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": "ABSTRACT"}]
    assert stderr == ""


def test_cli_reflection_metadata_compression_and_diagnostics(
    plaintext_server: RunningServer,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": "STABLE"}]
    assert "server reflection negotiated v1" in stderr
    assert any(
        method.startswith("/grpc.reflection.v1.ServerReflection/")
        for method, _metadata in running.metadata
    )


def test_cli_sanitizes_reflection_protocol_errors(
    schema: Schema,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 1
    assert output == []
    assert "reflection: 13: bad\\nforged\\rline\\x1b[2J\\u202eowned" in stderr
    assert "\x1b" not in stderr


def test_dns_resolver_sends_endpoint_as_reflection_host(
    schema: Schema,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": "AUTHORITY"}]
    assert stderr == ""


def test_cli_renders_rich_status_from_a_real_server(
    schema: Schema,
    plaintext_server: RunningServer,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
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
    capfd: pytest.CaptureFixture[str],
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

    captured = capfd.readouterr()
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
    capfd: pytest.CaptureFixture[str],
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

    captured = capfd.readouterr()
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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 1
    assert output == [{"text": "EXPORT"}]
    assert "protodial: (E8002)" in stderr
    assert "exported.pb" in stderr
    assert output_path.is_dir()


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
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 1, stderr.decode("utf-8", errors="replace")
    assert output == []
    assert "RESOURCE_EXHAUSTED" in stderr


def test_cli_enforces_rpc_deadline(
    schema: Schema,
    plaintext_server: RunningServer,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
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


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem_key(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Certificates:
    root = tmp_path_factory.mktemp("cli-e2e-tls")
    now = datetime.now(timezone.utc)
    ca_key = _key()
    ca_name = _name("protodial test CA")
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def leaf(
        common_name: str, usage: ExtendedKeyUsageOID
    ) -> tuple[bytes, bytes]:
        key = _key()
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        )
        if usage == ExtendedKeyUsageOID.SERVER_AUTH:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
        cert = builder.sign(ca_key, hashes.SHA256())
        return cert.public_bytes(serialization.Encoding.PEM), _pem_key(key)

    server_cert, server_key = leaf("localhost", ExtendedKeyUsageOID.SERVER_AUTH)
    client_cert, client_key = leaf(
        "protodial test client", ExtendedKeyUsageOID.CLIENT_AUTH
    )
    paths = {
        "ca": ca_cert.public_bytes(serialization.Encoding.PEM),
        "server_cert": server_cert,
        "server_key": server_key,
        "client_cert": client_cert,
        "client_key": client_key,
    }
    for name, contents in paths.items():
        (root / f"{name}.pem").write_bytes(contents)
    return Certificates(*(root / f"{name}.pem" for name in paths))


@pytest.mark.parametrize(
    "require_client_certificate", [False, True], ids=("tls", "mtls")
)
def test_cli_verified_tls_and_mtls(
    schema: Schema,
    certificates: Certificates,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0
    assert output == [{"text": "SECURE"}]
    assert stderr == ""


@pytest.mark.parametrize("use_tcp", [False, True])
def test_cli_insecure_tls_tunnel(
    schema: Schema,
    certificates: Certificates,
    capfd: pytest.CaptureFixture[str],
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

    output, stderr = _json_output(capfd)
    assert exit_code == 0, stderr
    assert output == [{"text": "TUNNEL"}]
    assert (
        "* warning (W3001): TLS certificate verification is disabled" in stderr
    )
