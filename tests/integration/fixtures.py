from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.rpc import error_details_pb2, status_pb2
from grpc_reflection.v1alpha import reflection
from grpc_tools import protoc

from .server import MetadataRecorder, _make_server
from .support import (
    PROTO,
    VALIDATOR_PROTO,
    VALIDATOR_SERVICE_NAME,
    RunningServer,
    Schema,
    ValidatorSchema,
)


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
