from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf import descriptor_pb2

from protodial.errors import CliError, SchemaInputError
from protodial.schema.local import (
    ProtoDescriptorSource,
    ProtosetDescriptorSource,
    local_descriptor_source,
)

from .support import _command, _file, _write_set


def test_multiple_proto_entrypoints_compile_with_shared_and_well_known_imports(
    tmp_path: Path,
) -> None:
    (tmp_path / "shared.proto").write_text(
        'syntax = "proto3"; package shared; '
        "message Value { string text = 1; }\n"
    )
    first = tmp_path / "first.proto"
    first.write_text(
        'syntax = "proto3"; package first; import "shared.proto"; '
        "service API { rpc Call(shared.Value) returns (shared.Value); }\n"
    )
    second = tmp_path / "second.proto"
    second.write_text(
        'syntax = "proto3"; package second; '
        'import "google/protobuf/timestamp.proto"; '
        "service API { rpc Call(google.protobuf.Timestamp) "
        "returns (google.protobuf.Timestamp); }\n"
    )
    schema = SimpleNamespace(
        proto=(str(first), str(second)),
        proto_path=(str(tmp_path),),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(_command(schema))

    assert source.list_services() == ("first.API", "second.API")
    assert (
        source.find_symbol("first.API/Call").input_type.full_name
        == "shared.Value"
    )
    assert (
        source.find_symbol("second.API/Call").input_type.full_name
        == "google.protobuf.Timestamp"
    )


def test_proto_entrypoints_with_the_same_basename_remain_distinct(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "api.proto"
    second = second_dir / "api.proto"
    first.write_text(
        'syntax = "proto3"; package first; message Request {} '
        "service API { rpc Call(Request) returns (Request); }\n"
    )
    second.write_text(
        'syntax = "proto3"; package second; message Request {} '
        "service API { rpc Call(Request) returns (Request); }\n"
    )
    schema = SimpleNamespace(
        proto=(str(first), str(second)),
        proto_path=(),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(_command(schema))

    assert source.list_services() == ("first.API", "second.API")
    assert source.find_symbol("first.Request").full_name == "first.Request"
    assert source.find_symbol("second.Request").full_name == "second.Request"


def test_protoset_indexes_files_not_reachable_from_services(
    tmp_path: Path,
) -> None:
    api = _file("api.proto", "api")
    orphan = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan.message_type.add(name="Hidden")
    source = ProtosetDescriptorSource(
        (_write_set(tmp_path / "schema.pb", api, orphan),)
    )

    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"


def test_semantically_invalid_protoset_names_its_input_path(
    tmp_path: Path,
) -> None:
    dependency = _file("dependency.proto", "dependency")
    invalid = _file("invalid.proto", "invalid")
    invalid.dependency.append("missing.proto")
    valid_path = _write_set(tmp_path / "valid.pb", dependency)
    invalid_path = _write_set(tmp_path / "invalid.pb", invalid)

    with pytest.raises(SchemaInputError) as raised:
        ProtosetDescriptorSource((valid_path, invalid_path))

    assert raised.value.path == invalid_path
    assert "invalid.proto" in (raised.value.reason or "")
    assert "missing.proto" in (raised.value.reason or "")


def test_protoset_dependency_may_be_supplied_by_a_later_path(
    tmp_path: Path,
) -> None:
    dependency = _file("dependency.proto", "dependency")
    dependent = _file("dependent.proto", "dependent")
    dependent.dependency.append(dependency.name)
    dependent_path = _write_set(tmp_path / "dependent.pb", dependent)
    dependency_path = _write_set(tmp_path / "dependency.pb", dependency)

    source = ProtosetDescriptorSource((dependent_path, dependency_path))

    assert set(source.file_protos) == {"dependent.proto", "dependency.proto"}


def test_mixed_proto_and_protosets_resolve_dependencies_across_all_sets(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "unrelated.proto"
    unrelated.write_text(
        'syntax = "proto3"; package unrelated; message Message {}\n'
    )
    dependency = _file("dependency.proto", "dependency")
    dependent = _file("dependent.proto", "dependent")
    dependent.dependency.append(dependency.name)
    dependent_path = _write_set(tmp_path / "dependent.pb", dependent)
    dependency_path = _write_set(tmp_path / "dependency.pb", dependency)
    schema = SimpleNamespace(
        proto=(str(unrelated),),
        proto_path=(str(tmp_path),),
        protoset=(dependent_path, dependency_path),
        protoset_out=None,
        use_reflection=False,
    )

    source = local_descriptor_source(_command(schema))

    assert set(source.file_protos) == {
        "unrelated.proto",
        "dependent.proto",
        "dependency.proto",
    }


def test_protoset_conflict_names_descriptor_and_both_origins(
    tmp_path: Path,
) -> None:
    first = _file("duplicate.proto", "one")
    second = _file("duplicate.proto", "two")
    first_path = _write_set(tmp_path / "first.pb", first)
    second_path = _write_set(tmp_path / "second.pb", second)

    with pytest.raises(CliError) as raised:
        ProtosetDescriptorSource((first_path, second_path))

    message = str(raised.value)
    assert "duplicate.proto" in message
    assert first_path in message
    assert second_path in message


def test_descriptors_differing_only_in_source_info_are_accepted(
    tmp_path: Path,
) -> None:
    first_proto = _file("same.proto", "same")
    second_proto = descriptor_pb2.FileDescriptorProto()
    second_proto.CopyFrom(first_proto)
    first_proto.source_code_info.location.add(
        path=(4, 0), leading_comments="first"
    )
    second_proto.source_code_info.location.add(
        path=(4, 0), leading_comments="second"
    )
    first = _write_set(tmp_path / "first.pb", first_proto)
    second = _write_set(tmp_path / "second.pb", second_proto)

    source = ProtosetDescriptorSource((first, second))

    assert source.list_services() == ("same.API",)


def test_identical_duplicate_protoset_files_are_accepted(
    tmp_path: Path,
) -> None:
    file_proto = _file("same.proto", "same")
    first = _write_set(tmp_path / "first.pb", file_proto)
    second = _write_set(tmp_path / "second.pb", file_proto)

    source = ProtosetDescriptorSource((first, second))

    assert source.list_services() == ("same.API",)
