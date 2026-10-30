from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.protobuf import descriptor_pool

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


def _json_output(
    capsys: pytest.CaptureFixture[str],
) -> tuple[list[dict[str, object]], str]:
    captured = capsys.readouterr()
    return [
        json.loads(line) for line in captured.out.splitlines()
    ], captured.err
