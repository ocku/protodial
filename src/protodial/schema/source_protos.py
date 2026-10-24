"""Recover source protos from runtime protobuf descriptors."""

from __future__ import annotations

from typing import Protocol, cast

from google.protobuf import descriptor_pool
from google.protobuf.descriptor import FieldDescriptor, FileDescriptor
from google.protobuf.descriptor_pb2 import (
    DescriptorProto,
    FieldDescriptorProto,
    FileDescriptorProto,
)

_FILE_PROTOS: dict[FileDescriptor, FileDescriptorProto] = {}
_POOL_FILE_PROTOS: dict[
    descriptor_pool.DescriptorPool, dict[str, FileDescriptorProto]
] = {}


class _FieldWithFile(Protocol):
    file: FileDescriptor


def register_file_proto(
    file_descriptor: FileDescriptor,
    file_proto: FileDescriptorProto,
) -> None:
    """Associate a runtime descriptor with its lossless source proto."""

    # FileDescriptor and DescriptorPool objects cannot be weak-referenced, and
    # the runtime strips SourceCodeInfo from FileDescriptor.serialized_pb.  A
    # wire-form key would therefore alias independent sources whose schemas are
    # identical but whose comments differ.  Keep the descriptor itself as a
    # strong identity key.  The deliberate lifetime cost avoids both recycled
    # ``id()`` collisions and eviction changing the rendering of live sources.
    stored = FileDescriptorProto()
    stored.CopyFrom(file_proto)
    _FILE_PROTOS[file_descriptor] = stored


def register_pool_file_protos(
    pool: descriptor_pool.DescriptorPool,
    file_protos: dict[str, FileDescriptorProto],
) -> None:
    """Associate a pool with its live source-proto mapping."""

    # DescriptorPool cannot be weak-referenced. Keep both the pool and its
    # source mapping alive so descriptors obtained directly from public pool
    # APIs can recover metadata added lazily by reflection. As with the
    # descriptor-specific registry above, this deliberate lifetime cost avoids
    # recycled ``id()`` collisions and preserves source isolation.
    _POOL_FILE_PROTOS[pool] = file_protos


def source_file_proto(
    file_descriptor: FileDescriptor,
) -> FileDescriptorProto | None:
    """Return the registered lossless source proto, if one is available."""

    source = _FILE_PROTOS.get(file_descriptor)
    if source is not None:
        return source
    pool = getattr(file_descriptor, "pool", None)
    if pool is not None:
        source = _POOL_FILE_PROTOS.get(pool, {}).get(file_descriptor.name)
        if source is not None:
            return source
    serialized = getattr(file_descriptor, "serialized_pb", None)
    if not serialized:
        return None
    return FileDescriptorProto.FromString(serialized)


def field_proto(field: FieldDescriptor) -> FieldDescriptorProto:
    """Return the source proto for a runtime field descriptor."""

    file_descriptor = cast(_FieldWithFile, field).file
    file_proto = source_file_proto(file_descriptor)
    if file_proto is None:
        file_proto = FileDescriptorProto()
        file_descriptor.CopyToProto(file_proto)

    def qualify(parent: str, name: str) -> str:
        return f"{parent}.{name}" if parent else name

    def walk(
        message: DescriptorProto, parent: str
    ) -> FieldDescriptorProto | None:
        message_name = qualify(parent, message.name)
        for candidate in (*message.field, *message.extension):
            if (
                qualify(message_name, candidate.name) == field.full_name
                and candidate.number == field.number
            ):
                return candidate
        for nested in message.nested_type:
            found = walk(nested, message_name)
            if found is not None:
                return found
        return None

    for message in file_proto.message_type:
        found = walk(message, file_proto.package)
        if found is not None:
            return found
    for candidate in file_proto.extension:
        if (
            qualify(file_proto.package, candidate.name) == field.full_name
            and candidate.number == field.number
        ):
            return candidate
    raise ValueError(f"could not locate field proto for {field.full_name}")
