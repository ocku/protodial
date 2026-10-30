"""Render protobuf descriptors as concise source-like text."""

from __future__ import annotations

import json
from typing import Protocol, cast

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import (
    Descriptor,
    EnumDescriptor,
    EnumValueDescriptor,
    FieldDescriptor,
    FileDescriptor,
    MethodDescriptor,
    OneofDescriptor,
    ServiceDescriptor,
)
from google.protobuf.message import Message

from ..schema.contracts import DescriptorSymbol as DescriptorLike
from ..schema.inspection import SCALAR_TYPES as _SCALAR_TYPES
from ..schema.inspection import is_map as _is_map
from ..schema.inspection import is_synthetic_oneof as _is_synthetic_oneof
from ..schema.source_comments import comments_for_file as _comments_for_file
from ..schema.source_protos import field_proto as _field_proto
from ..schema.source_protos import source_file_proto

DeclarationProto = (
    descriptor_pb2.DescriptorProto | descriptor_pb2.EnumDescriptorProto
)


class _DescriptorWithFile(Protocol):
    file: FileDescriptor


class _FullNamedDescriptor(Protocol):
    full_name: str


def render_descriptor(descriptor: DescriptorLike) -> str:
    """Render a protobuf descriptor as source-like text."""
    if isinstance(descriptor, OneofDescriptor):
        file_descriptor = descriptor.containing_type.file
    elif isinstance(descriptor, MethodDescriptor):
        file_descriptor = descriptor.containing_service.file
    elif isinstance(descriptor, EnumValueDescriptor):
        file_descriptor = descriptor.type.file
    else:
        file_descriptor = cast(_DescriptorWithFile, descriptor).file
    comments = _comments_for_file(file_descriptor)
    if isinstance(descriptor, ServiceDescriptor):
        return _render_service(descriptor, comments)
    if isinstance(descriptor, MethodDescriptor):
        return _with_comment(descriptor, _render_method(descriptor), comments)
    if isinstance(descriptor, EnumDescriptor):
        return _render_enum(descriptor, comments, descriptor.full_name)
    if isinstance(descriptor, Descriptor):
        return _render_message(descriptor, comments, descriptor.full_name)
    if isinstance(descriptor, OneofDescriptor):
        if _is_synthetic_oneof(descriptor):
            field = descriptor.fields[0]
            return _with_comment(field, _render_field(field), comments)
        return _render_oneof(descriptor, comments)
    if isinstance(descriptor, FieldDescriptor):
        return _render_field_declaration(descriptor, comments)
    return _render_enum_value(descriptor, comments)


def _render_service(
    service: ServiceDescriptor, comments: dict[str, str]
) -> str:
    lines = [f"service {service.full_name} {{"]
    if service.GetOptions().deprecated:
        lines.append("  option deprecated = true;")
    for method in service.methods:
        lines.extend(
            _indent(
                _with_comment(
                    method, _render_method(method), comments
                ).splitlines()
            )
        )
    lines.append("}")
    return _with_comment(service, "\n".join(lines), comments)


def _render_method(method: MethodDescriptor) -> str:
    request_stream = "stream " if method.client_streaming else ""
    response_stream = "stream " if method.server_streaming else ""
    declaration = (
        f"rpc {method.name} ({request_stream}.{method.input_type.full_name}) "
        f"returns ({response_stream}.{method.output_type.full_name})"
    )
    options = _option_assignments(
        method.GetOptions(), ("deprecated", "idempotency_level")
    )
    if not options:
        return declaration + ";"
    body = "\n".join(f"  option {option};" for option in options)
    return f"{declaration} {{\n{body}\n}}"


def _render_enum(
    enum: EnumDescriptor, comments: dict[str, str], display_name: str
) -> str:
    lines = [f"enum {display_name} {{"]
    option = _standalone_option(enum.GetOptions(), "allow_alias")
    if option:
        lines.append(f"  {option}")
    if enum.GetOptions().deprecated:
        lines.append("  option deprecated = true;")
    proto = _copy_proto(enum)
    for reserved_range in proto.reserved_range:
        lines.append(
            "  reserved "
            f"{_enum_range(reserved_range.start, reserved_range.end)};"
        )
    if proto.reserved_name:
        names = ", ".join(json.dumps(name) for name in proto.reserved_name)
        lines.append(f"  reserved {names};")
    for value in enum.values:
        lines.extend(_indent(_render_enum_value(value, comments).splitlines()))
    lines.append("}")
    return _with_comment(enum, "\n".join(lines), comments)


def _render_enum_value(
    value: EnumValueDescriptor, comments: dict[str, str]
) -> str:
    options = _option_assignments(value.GetOptions(), ("deprecated",))
    suffix = f" [{', '.join(options)}]" if options else ""
    rendered = f"{value.name} = {value.number}{suffix};"
    return _with_comment(value, rendered, comments)


def _render_message(
    message: Descriptor, comments: dict[str, str], display_name: str
) -> str:
    lines = [f"message {display_name} {{"]
    proto = cast(descriptor_pb2.DescriptorProto, _copy_proto(message))
    lines.extend(_message_declaration_lines(message, proto))
    lines.extend(_message_nested_declarations(message, comments))
    lines.extend(_message_fields(message, comments))
    lines.extend(_message_extensions(message, comments))
    lines.append("}")
    return _with_comment(message, "\n".join(lines), comments)


def _message_declaration_lines(
    message: Descriptor, proto: descriptor_pb2.DescriptorProto
) -> list[str]:
    lines: list[str] = []
    if message.GetOptions().deprecated:
        lines.append("  option deprecated = true;")
    for reserved_range in proto.reserved_range:
        lines.append(
            f"  reserved {_range(reserved_range.start, reserved_range.end)};"
        )
    if proto.reserved_name:
        names = ", ".join(json.dumps(name) for name in proto.reserved_name)
        lines.append(f"  reserved {names};")
    for start, end in message.extension_ranges:
        lines.append(f"  extensions {_range(start, end)};")
    return lines


def _message_nested_declarations(
    message: Descriptor, comments: dict[str, str]
) -> list[str]:
    lines: list[str] = []
    for enum in message.enum_types:
        lines.extend(
            _indent(_render_enum(enum, comments, enum.name).splitlines())
        )
    group_types = {
        field.message_type.full_name
        for field in (*message.fields, *message.extensions)
        if field.type == FieldDescriptor.TYPE_GROUP
        and field.message_type is not None
    }
    for nested in message.nested_types:
        if (
            not nested.GetOptions().map_entry
            and nested.full_name not in group_types
        ):
            lines.extend(
                _indent(
                    _render_message(nested, comments, nested.name).splitlines()
                )
            )
    return lines


def _render_field_declaration(
    field: FieldDescriptor, comments: dict[str, str]
) -> str:
    rendered = (
        _render_group(field, comments)
        if field.type == FieldDescriptor.TYPE_GROUP
        else _render_field(field)
    )
    if field.is_extension:
        target = field.containing_type
        if target is None:
            raise ValueError(f"extension {field.full_name} has no extendee")
        body = "\n".join(_indent(rendered.splitlines()))
        rendered = f"extend .{target.full_name} {{\n{body}\n}}"
    return _with_comment(field, rendered, comments)


def _message_fields(message: Descriptor, comments: dict[str, str]) -> list[str]:
    real_oneofs = {
        oneof.name: oneof
        for oneof in message.oneofs
        if not _is_synthetic_oneof(oneof)
    }
    lines: list[str] = []
    rendered_oneofs: set[str] = set()
    for field in message.fields:
        oneof = field.containing_oneof
        if oneof is not None and oneof.name in real_oneofs:
            if oneof.name in rendered_oneofs:
                continue
            rendered_oneofs.add(oneof.name)
            lines.extend(_indent(_render_oneof(oneof, comments).splitlines()))
        else:
            lines.extend(
                _indent(_render_field_declaration(field, comments).splitlines())
            )
    return lines


def _message_extensions(
    message: Descriptor, comments: dict[str, str]
) -> list[str]:
    lines: list[str] = []
    for extension in message.extensions:
        lines.extend(
            _indent(_render_field_declaration(extension, comments).splitlines())
        )
    return lines


def _render_group(field: FieldDescriptor, comments: dict[str, str]) -> str:
    group = field.message_type
    if group is None:
        raise ValueError(f"group field {field.full_name} has no message type")
    if field.is_repeated:
        label = "repeated"
    elif field.is_required:
        label = "required"
    else:
        label = "optional"
    message_opening = f"message {group.name} {{"
    group_opening = f"{label} group {group.name} = {field.number} {{"
    return _render_message(group, comments, group.name).replace(
        message_opening, group_opening, 1
    )


def _render_oneof(oneof: OneofDescriptor, comments: dict[str, str]) -> str:
    lines = [f"oneof {oneof.name} {{"]
    for field in oneof.fields:
        lines.extend(
            _indent(_render_field_declaration(field, comments).splitlines())
        )
    lines.append("}")
    return _with_comment(oneof, "\n".join(lines), comments)


def _render_field(field: FieldDescriptor) -> str:
    field_proto = _field_proto(field)
    if _is_map(field):
        map_type = field.message_type
        if map_type is None:
            raise ValueError(f"map field {field.full_name} has no entry type")
        key, value = map_type.fields
        type_name = f"map<{_field_type(key)}, {_field_type(value)}>"
        label = ""
    else:
        type_name = _field_type(field)
        if field.is_repeated:
            label = "repeated "
        elif field.is_required:
            label = "required "
        elif field_proto.proto3_optional:
            label = "optional "
        elif field.containing_oneof is not None:
            label = ""
        elif _file_syntax(cast(_DescriptorWithFile, field).file) == "proto2":
            label = "optional "
        else:
            label = ""

    options: list[str] = []
    if field_proto.HasField("default_value"):
        options.append(
            f"default = {_format_default(field, field_proto.default_value)}"
        )
    field_options = field.GetOptions()
    if field_options.deprecated:
        options.append("deprecated = true")
    if field_options.HasField("packed"):
        options.append(f"packed = {str(field_options.packed).lower()}")
    if field.json_name != _default_json_name(field.name):
        options.append(f"json_name = {json.dumps(field.json_name)}")
    suffix = f" [{', '.join(options)}]" if options else ""
    return f"{label}{type_name} {field.name} = {field.number}{suffix};"


def _field_type(field: FieldDescriptor) -> str:
    referenced = field.message_type or field.enum_type
    return (
        f".{referenced.full_name}"
        if referenced is not None
        else _SCALAR_TYPES[field.type]
    )


def _copy_proto(descriptor: Descriptor | EnumDescriptor) -> DeclarationProto:
    source_file = source_file_proto(descriptor.file)
    if source_file is not None:
        found = _find_declaration_proto(source_file, descriptor.full_name)
        if found is not None:
            return found
    if isinstance(descriptor, Descriptor):
        message_proto = descriptor_pb2.DescriptorProto()
        descriptor.CopyToProto(message_proto)
        return message_proto
    enum_proto = descriptor_pb2.EnumDescriptorProto()
    descriptor.CopyToProto(enum_proto)
    return enum_proto


def _find_declaration_proto(
    file_proto: descriptor_pb2.FileDescriptorProto, full_name: str
) -> DeclarationProto | None:
    def qualify(parent: str, name: str) -> str:
        return f"{parent}.{name}" if parent else name

    def walk_message(
        message: descriptor_pb2.DescriptorProto, parent: str
    ) -> DeclarationProto | None:
        message_name = qualify(parent, message.name)
        if message_name == full_name:
            return message
        for nested in message.nested_type:
            found = walk_message(nested, message_name)
            if found is not None:
                return found
        for enum in message.enum_type:
            if qualify(message_name, enum.name) == full_name:
                return enum
        return None

    for message in file_proto.message_type:
        found = walk_message(message, file_proto.package)
        if found is not None:
            return found
    for enum in file_proto.enum_type:
        if qualify(file_proto.package, enum.name) == full_name:
            return enum
    return None


def _file_syntax(file_descriptor: FileDescriptor) -> str:
    proto = source_file_proto(file_descriptor)
    if proto is None:
        proto = descriptor_pb2.FileDescriptorProto()
        file_descriptor.CopyToProto(proto)
    return proto.syntax or "proto2"


def _range(start: int, exclusive_end: int) -> str:
    end = "max" if exclusive_end >= 536_870_912 else str(exclusive_end - 1)
    return str(start) if exclusive_end == start + 1 else f"{start} to {end}"


def _enum_range(start: int, inclusive_end: int) -> str:
    end = "max" if inclusive_end >= 2_147_483_647 else str(inclusive_end)
    return str(start) if inclusive_end == start else f"{start} to {end}"


def _format_default(field: FieldDescriptor, value: str) -> str:
    if field.type == FieldDescriptor.TYPE_BYTES:
        # Descriptor protos store bytes defaults in protobuf's C-escaped form.
        # JSON encoding would escape those backslashes a second time.
        return f'"{value}"'
    if field.type == FieldDescriptor.TYPE_STRING:
        return json.dumps(value)
    if field.type == FieldDescriptor.TYPE_BOOL:
        return value.lower()
    return value


def _default_json_name(name: str) -> str:
    pieces = name.split("_")
    return pieces[0] + "".join(
        piece[:1].upper() + piece[1:] for piece in pieces[1:]
    )


def _option_assignments(options: Message, names: tuple[str, ...]) -> list[str]:
    rendered: list[str] = []
    for name in names:
        if not options.HasField(name):
            continue
        value = getattr(options, name)
        if name == "idempotency_level":
            method_options = cast(descriptor_pb2.MethodOptions, options)
            value = method_options.IdempotencyLevel.Name(value)
        elif isinstance(value, bool):
            value = str(value).lower()
        rendered.append(f"{name} = {value}")
    return rendered


def _standalone_option(options: Message, name: str) -> str | None:
    assignments = _option_assignments(options, (name,))
    return f"option {assignments[0]};" if assignments else None


def _indent(lines: list[str]) -> list[str]:
    return [f"  {line}" if line else "" for line in lines]


def _with_comment(
    descriptor: DescriptorLike, rendered: str, comments: dict[str, str]
) -> str:
    if isinstance(descriptor, EnumValueDescriptor):
        full_name = f"{descriptor.type.full_name}.{descriptor.name}"
    else:
        full_name = cast(_FullNamedDescriptor, descriptor).full_name
    comment = comments.get(full_name)
    if not comment:
        return rendered
    lines: list[str] = []
    for line in comment.strip().splitlines():
        lines.append(f"// {line.rstrip()}" if line.strip() else "//")
    lines.append(rendered)
    return "\n".join(lines)
