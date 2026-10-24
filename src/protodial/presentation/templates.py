"""Generate JSON request templates from message descriptors."""

from __future__ import annotations

import json
from typing import TypeAlias

from google.protobuf.descriptor import (
    Descriptor,
    FieldDescriptor,
    OneofDescriptor,
)

from ..schema.source_protos import field_proto

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


def _is_map(field: FieldDescriptor) -> bool:
    return bool(
        field.is_repeated
        and field.message_type is not None
        and field.message_type.GetOptions().map_entry
    )


def _is_synthetic_oneof(oneof: OneofDescriptor) -> bool:
    field = oneof.fields[0] if len(oneof.fields) == 1 else None
    if field is None:
        return False
    return field_proto(field).proto3_optional


def message_template(descriptor: Descriptor, max_depth: int = 8) -> str:
    """Return an example JSON object for a message descriptor."""
    value = _template_message(descriptor, (), max_depth)
    return json.dumps(value, ensure_ascii=False)


def _template_message(
    descriptor: Descriptor,
    stack: tuple[str, ...],
    depth: int,
) -> JSONValue:
    well_known: dict[str, JSONValue] = {
        "google.protobuf.Timestamp": "1970-01-01T00:00:00Z",
        "google.protobuf.Duration": "0s",
        "google.protobuf.FieldMask": "field.path",
        "google.protobuf.Empty": {},
        "google.protobuf.Value": None,
        "google.protobuf.Struct": {},
        "google.protobuf.ListValue": [],
        "google.protobuf.Any": {},
        "google.protobuf.DoubleValue": 0.0,
        "google.protobuf.FloatValue": 0.0,
        "google.protobuf.Int64Value": "0",
        "google.protobuf.UInt64Value": "0",
        "google.protobuf.Int32Value": 0,
        "google.protobuf.UInt32Value": 0,
        "google.protobuf.BoolValue": False,
        "google.protobuf.StringValue": "",
        "google.protobuf.BytesValue": "",
    }
    if descriptor.full_name in well_known:
        return well_known[descriptor.full_name]
    if descriptor.full_name in stack or depth <= 0:
        return {}

    result: dict[str, JSONValue] = {}
    next_stack = (*stack, descriptor.full_name)
    for field in descriptor.fields:
        oneof = field.containing_oneof
        if (
            oneof is not None
            and not _is_synthetic_oneof(oneof)
            and field is not oneof.fields[0]
        ):
            continue
        value = _template_field(field, next_stack, depth - 1)
        if _is_map(field):
            map_type = field.message_type
            if map_type is None:
                raise ValueError(
                    f"map field {field.full_name} has no entry type"
                )
            key_field, value_field = map_type.fields
            key = _template_scalar(key_field)
            value = _template_field(value_field, next_stack, depth - 1)
            map_key = str(key).lower() if isinstance(key, bool) else str(key)
            value = {map_key: value}
        elif field.is_repeated:
            value = [value]
        result[field.json_name] = value
    return result


def _template_field(
    field: FieldDescriptor,
    stack: tuple[str, ...],
    depth: int,
) -> JSONValue:
    if field.message_type is not None:
        return _template_message(field.message_type, stack, depth)
    if field.enum_type is not None:
        if field.enum_type.full_name == "google.protobuf.NullValue":
            return None
        return field.enum_type.values[0].name if field.enum_type.values else 0
    return _template_scalar(field)


def _template_scalar(field: FieldDescriptor) -> JSONValue:
    from google.protobuf.descriptor import FieldDescriptor

    if field.type == FieldDescriptor.TYPE_BOOL:
        return False
    if field.type == FieldDescriptor.TYPE_STRING:
        return ""
    if field.type == FieldDescriptor.TYPE_BYTES:
        return ""
    if field.type in (FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE):
        return 0.0
    if field.type in (
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_SFIXED64,
        FieldDescriptor.TYPE_SINT64,
    ):
        return "0"
    return 0
