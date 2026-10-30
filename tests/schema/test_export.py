from __future__ import annotations

from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

import protodial.schema.export as export_core
from protodial.schema.export import write_descriptor_set
from protodial.schema.local import (
    ProtosetDescriptorSource,
)

from .support import _file, _write_set


def test_descriptor_set_export_is_complete_dependency_ordered_and_atomic(
    tmp_path: Path,
) -> None:
    dependency = descriptor_pb2.FileDescriptorProto(
        name="dependency.proto", package="dependency", syntax="proto3"
    )
    dependency.message_type.add(name="Message")
    api = descriptor_pb2.FileDescriptorProto(
        name="api.proto", package="api", syntax="proto3"
    )
    api.dependency.append("dependency.proto")
    service = api.service.add(name="API")
    service.method.add(
        name="Call",
        input_type=".dependency.Message",
        output_type=".dependency.Message",
    )
    source_path = _write_set(tmp_path / "input.pb", api, dependency)
    source = ProtosetDescriptorSource((source_path,))
    output = tmp_path / "nested" / "schema.pb"

    write_descriptor_set(source, output)

    exported = descriptor_pb2.FileDescriptorSet.FromString(output.read_bytes())
    assert [file_proto.name for file_proto in exported.file] == [
        "dependency.proto",
        "api.proto",
    ]
    assert list(output.parent.glob(f".{output.name}.*")) == []


def test_descriptor_export_preserves_primary_when_temporary_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ProtosetDescriptorSource(
        (_write_set(tmp_path / "input.pb", _file("api.proto", "api")),)
    )
    output = tmp_path / "schema.pb"
    primary = OSError("replace primary")
    real_unlink = Path.unlink

    def fail_replace(_source: object, _destination: object) -> None:
        raise primary

    def fail_temporary_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(f".{output.name}."):
            raise PermissionError("unlink cleanup")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(export_core.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)

    with pytest.raises(OSError, match="replace primary") as raised:
        write_descriptor_set(source, output)

    assert raised.value is primary
    assert getattr(primary, "_protodial_notes") == (
        "descriptor temporary-file cleanup also failed: unlink cleanup",
    )
