import io
import struct

import pytest

from protodial.argument_parser import parse_target
from protodial.errors import CliError
from protodial.rpc import (
    parse_metadata,
    read_exact,
)


def test_target_url_rejects_invalid_path_and_query() -> None:
    with pytest.raises(CliError, match="/SERVICE/METHOD"):
        parse_target("grpc://localhost:50051/example.Greeter")
    with pytest.raises(CliError, match="query"):
        parse_target("grpc://localhost:50051/example.Greeter/SayHello?x=1")


def test_parse_metadata_decodes_binary_values() -> None:
    assert parse_metadata(["Token: value", "trace-bin: YWJj"]) == [
        ("token", "value"),
        ("trace-bin", b"abc"),
    ]


def test_parse_metadata_rejects_malformed_values() -> None:
    with pytest.raises(CliError, match="key:value"):
        parse_metadata(["missing-separator"])


def test_read_exact_reads_framed_payload() -> None:
    source = io.BytesIO(struct.pack(">I", 3) + b"abc")

    assert read_exact(source, 4) == struct.pack(">I", 3)
    assert read_exact(source, 3) == b"abc"
    assert read_exact(source, 1, allow_eof=True) is None


def test_read_exact_rejects_truncated_payload() -> None:
    with pytest.raises(CliError, match="truncated"):
        read_exact(io.BytesIO(b"ab"), 3)
