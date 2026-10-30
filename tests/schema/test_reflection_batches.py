from __future__ import annotations

import pytest
from google.protobuf import descriptor_database, descriptor_pb2, descriptor_pool

from protodial.errors import CliError
from protodial.schema.export import descriptor_set
from protodial.schema.reflection import (
    reflection_database,
)
from protodial.schema.registry import (
    IndexedDescriptorSource,
)

from .support import _DescriptorBatchChannel, _grpc


@pytest.mark.parametrize(
    "payload",
    [
        descriptor_pb2.FileDescriptorProto().SerializeToString(),
        descriptor_pb2.FileDescriptorProto(
            name="invalid.proto", syntax="not-a-proto-syntax"
        ).SerializeToString(),
    ],
)
def test_reflection_rejects_semantically_invalid_descriptor_payload(
    payload: bytes,
) -> None:
    database = reflection_database(
        _DescriptorBatchChannel((payload,)), [], 1.0, _grpc()
    )

    with pytest.raises(CliError) as raised:
        database.query(file_by_filename="invalid.proto")

    assert str(raised.value) == (
        "reflection protocol error: invalid file descriptor payload"
    )
    assert raised.value.__cause__ is None
    assert database.file_protos == {}


@pytest.mark.parametrize(
    "bad_payload",
    [
        b"\xff",
        descriptor_pb2.FileDescriptorProto(
            name="invalid.proto", syntax="invalid"
        ).SerializeToString(),
    ],
)
def test_reflection_descriptor_batch_is_transactional(
    bad_payload: bytes,
) -> None:
    committed = descriptor_pb2.FileDescriptorProto(
        name="committed.proto", syntax="proto3"
    )
    candidate = descriptor_pb2.FileDescriptorProto(
        name="candidate.proto", syntax="proto3"
    )
    database = reflection_database(
        _DescriptorBatchChannel(
            (committed.SerializeToString(),),
            (candidate.SerializeToString(), bad_payload),
        ),
        [],
        1.0,
        _grpc(),
    )
    database.query(file_by_filename=committed.name)

    with pytest.raises(CliError, match="invalid file descriptor payload"):
        database.query(file_by_filename=candidate.name)

    assert tuple(database.file_protos) == (committed.name,)
    with pytest.raises(KeyError):
        descriptor_database.DescriptorDatabase.FindFileByName(
            database, candidate.name
        )
    source = IndexedDescriptorSource(
        descriptor_pool.DescriptorPool(database), (), database.file_protos
    )
    assert [file.name for file in descriptor_set(source).file] == [
        committed.name
    ]


def test_reflection_conflicting_batch_is_transactional() -> None:
    committed = descriptor_pb2.FileDescriptorProto(
        name="shared.proto", package="original", syntax="proto3"
    )
    candidate = descriptor_pb2.FileDescriptorProto(
        name="candidate.proto", syntax="proto3"
    )
    conflict = descriptor_pb2.FileDescriptorProto(
        name=committed.name, package="conflict", syntax="proto3"
    )
    database = reflection_database(
        _DescriptorBatchChannel(
            (committed.SerializeToString(),),
            (candidate.SerializeToString(), conflict.SerializeToString()),
        ),
        [],
        1.0,
        _grpc(),
    )
    database.query(file_by_filename=committed.name)

    with pytest.raises(CliError, match="invalid file descriptor payload"):
        database.query(file_by_filename=candidate.name)

    assert tuple(database.file_protos) == (committed.name,)
    assert database.file_protos[committed.name].package == "original"
    with pytest.raises(KeyError):
        descriptor_database.DescriptorDatabase.FindFileByName(
            database, candidate.name
        )


def test_reflection_descriptor_batch_allows_late_dependency() -> None:
    dependency = descriptor_pb2.FileDescriptorProto(
        name="dependency.proto", package="dependency", syntax="proto3"
    )
    dependency.message_type.add(name="Message")
    dependent = descriptor_pb2.FileDescriptorProto(
        name="dependent.proto", package="dependent", syntax="proto3"
    )
    dependent.dependency.append(dependency.name)
    dependent.message_type.add(name="UsesDependency").field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".dependency.Message",
    )
    database = reflection_database(
        _DescriptorBatchChannel(
            (dependent.SerializeToString(), dependency.SerializeToString())
        ),
        [],
        1.0,
        _grpc(),
    )

    database.query(file_by_filename=dependent.name)

    pool = descriptor_pool.DescriptorPool(database)
    assert pool.FindFileByName(dependent.name).name == dependent.name
    assert tuple(database.file_protos) == (dependent.name, dependency.name)


def test_reflection_identical_descriptor_duplicates_remain_idempotent() -> None:
    proto = descriptor_pb2.FileDescriptorProto(
        name="same.proto", syntax="proto3"
    )
    payload = proto.SerializeToString()
    database = reflection_database(
        _DescriptorBatchChannel((payload, payload), (payload,)),
        [],
        1.0,
        _grpc(),
    )

    database.query(file_by_filename=proto.name)
    database.query(file_by_filename=proto.name)

    assert tuple(database.file_protos) == (proto.name,)
