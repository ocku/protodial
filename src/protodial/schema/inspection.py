"""Shared semantic properties of protobuf runtime descriptors."""

from __future__ import annotations

from google.protobuf.descriptor import FieldDescriptor, OneofDescriptor

from .source_protos import field_proto

SCALAR_TYPES = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    6: "fixed64",
    7: "fixed32",
    8: "bool",
    9: "string",
    12: "bytes",
    13: "uint32",
    15: "sfixed32",
    16: "sfixed64",
    17: "sint32",
    18: "sint64",
}


def is_map(field: FieldDescriptor) -> bool:
    """Return whether a field encodes a protobuf map entry."""
    return bool(
        field.is_repeated
        and field.message_type is not None
        and field.message_type.GetOptions().map_entry
    )


def is_synthetic_oneof(oneof: OneofDescriptor) -> bool:
    """Return whether a oneof carries proto3 optional-field presence."""
    return (
        len(oneof.fields) == 1 and field_proto(oneof.fields[0]).proto3_optional
    )
