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

from ..schema.source_protos import source_file_proto

DescriptorLike = (
    Descriptor
    | EnumDescriptor
    | EnumValueDescriptor
    | FieldDescriptor
    | MethodDescriptor
    | OneofDescriptor
    | ServiceDescriptor
)
DeclarationProto = (
    descriptor_pb2.DescriptorProto | descriptor_pb2.EnumDescriptorProto
)


class _DescriptorWithFile(Protocol):
    file: FileDescriptor


class _FullNamedDescriptor(Protocol):
    full_name: str


_SCALAR_TYPES = {
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


def render_descriptor(descriptor: DescriptorLike) -> str:
    """Render a protobuf descriptor as source-like text."""
    if isinstance(descriptor, OneofDescriptor):
        file_descriptor = descriptor.containing_type.file
    elif isinstance(descriptor, MethodDescriptor):
        file_descriptor = descriptor.containing_service.file
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
        line = _render_field(descriptor)
        if descriptor.is_extension:
            target = descriptor.containing_type
            if target is None:
                raise ValueError(
                    f"extension {descriptor.full_name} has no extendee"
                )
            line = f"extend .{target.full_name} {{\n  {line}\n}}"
        return _with_comment(descriptor, line, comments)
    return _with_comment(
        descriptor,
        f"{descriptor.name} = {descriptor.number};",
        comments,
    )


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
    options = _option_suffix(
        method.GetOptions(), ("deprecated", "idempotency_level")
    )
    return (
        f"rpc {method.name} ({request_stream}.{method.input_type.full_name}) "
        f"returns ({response_stream}.{method.output_type.full_name}){options};"
    )


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
        suffix = _option_suffix(value.GetOptions(), ("deprecated",))
        rendered = f"{value.name} = {value.number}{suffix};"
        lines.extend(
            _indent(_with_comment(value, rendered, comments).splitlines())
        )
    lines.append("}")
    return _with_comment(enum, "\n".join(lines), comments)


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
        for field in message.fields
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


def _render_message_field(
    field: FieldDescriptor, comments: dict[str, str]
) -> list[str]:
    rendered = (
        _render_group(field, comments)
        if field.type == FieldDescriptor.TYPE_GROUP
        else _render_field(field)
    )
    return _indent(_with_comment(field, rendered, comments).splitlines())


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
            lines.extend(_render_message_field(field, comments))
    return lines


def _message_extensions(
    message: Descriptor, comments: dict[str, str]
) -> list[str]:
    lines: list[str] = []
    for extension in message.extensions:
        target_type = extension.containing_type
        if target_type is None:
            raise ValueError(f"extension {extension.full_name} has no extendee")
        block = (
            f"extend .{target_type.full_name} {{\n"
            f"  {_render_field(extension)}\n"
            "}"
        )
        lines.extend(
            _indent(_with_comment(extension, block, comments).splitlines())
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
        rendered = (
            _render_group(field, comments)
            if field.type == FieldDescriptor.TYPE_GROUP
            else _render_field(field)
        )
        lines.extend(
            _indent(_with_comment(field, rendered, comments).splitlines())
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


def _is_map(field: FieldDescriptor) -> bool:
    return bool(
        field.is_repeated
        and field.message_type is not None
        and field.message_type.GetOptions().map_entry
    )


def _is_synthetic_oneof(oneof: OneofDescriptor) -> bool:
    return (
        len(oneof.fields) == 1 and _field_proto(oneof.fields[0]).proto3_optional
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


def _field_proto(field: FieldDescriptor) -> descriptor_pb2.FieldDescriptorProto:
    if field.is_extension:
        if field.extension_scope is not None:
            scope_proto = _copy_proto(field.extension_scope)
            if not isinstance(scope_proto, descriptor_pb2.DescriptorProto):
                raise TypeError("extension scope must be a message")
            candidates = scope_proto.extension
        else:
            file_descriptor = cast(_DescriptorWithFile, field).file
            file_proto = source_file_proto(file_descriptor)
            if file_proto is None:
                file_proto = descriptor_pb2.FileDescriptorProto()
                file_descriptor.CopyToProto(file_proto)
            candidates = file_proto.extension
    else:
        containing_type = field.containing_type
        if containing_type is None:
            raise ValueError(
                f"field {field.full_name} has no containing message"
            )
        message_proto = _copy_proto(containing_type)
        if not isinstance(message_proto, descriptor_pb2.DescriptorProto):
            raise TypeError("field container must be a message")
        candidates = message_proto.field
    for candidate in candidates:
        if candidate.name == field.name and candidate.number == field.number:
            return candidate
    raise ValueError(f"could not locate field proto for {field.full_name}")


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


def _option_suffix(options: Message, names: tuple[str, ...]) -> str:
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
    return f" [{', '.join(rendered)}]" if rendered else ""


def _standalone_option(options: Message, name: str) -> str | None:
    if options.HasField(name):
        value = getattr(options, name)
        return f"option {name} = {str(value).lower()};"
    return None


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


def _comments_for_file(file_descriptor: FileDescriptor) -> dict[str, str]:
    proto = source_file_proto(file_descriptor)
    if proto is None:
        proto = descriptor_pb2.FileDescriptorProto()
        file_descriptor.CopyToProto(proto)
    locations = {
        tuple(location.path): location
        for location in proto.source_code_info.location
    }
    comments: dict[str, str] = {}

    def save(full_name: str, path: tuple[int, ...]) -> None:
        location = locations.get(path)
        if location is None:
            return
        text = location.leading_comments or location.trailing_comments
        if text:
            comments[full_name] = text

    package = proto.package

    def qualify(parent: str, name: str) -> str:
        return f"{parent}.{name}" if parent else name

    def walk_enum(
        enum: descriptor_pb2.EnumDescriptorProto,
        path: tuple[int, ...],
        parent: str,
    ) -> None:
        full_name = qualify(parent, enum.name)
        save(full_name, path)
        for index, value in enumerate(enum.value):
            save(qualify(full_name, value.name), path + (2, index))

    def walk_message(
        message: descriptor_pb2.DescriptorProto,
        path: tuple[int, ...],
        parent: str,
    ) -> None:
        full_name = qualify(parent, message.name)
        save(full_name, path)
        for index, field in enumerate(message.field):
            save(qualify(full_name, field.name), path + (2, index))
        for index, nested in enumerate(message.nested_type):
            walk_message(nested, path + (3, index), full_name)
        for index, enum in enumerate(message.enum_type):
            walk_enum(enum, path + (4, index), full_name)
        for index, extension in enumerate(message.extension):
            save(qualify(full_name, extension.name), path + (6, index))
        for index, oneof in enumerate(message.oneof_decl):
            save(qualify(full_name, oneof.name), path + (8, index))

    for index, message in enumerate(proto.message_type):
        walk_message(message, (4, index), package)
    for index, enum in enumerate(proto.enum_type):
        walk_enum(enum, (5, index), package)
    for service_index, service in enumerate(proto.service):
        full_name = qualify(package, service.name)
        save(full_name, (6, service_index))
        for method_index, method in enumerate(service.method):
            save(
                qualify(full_name, method.name),
                (6, service_index, 2, method_index),
            )
    for index, extension in enumerate(proto.extension):
        save(qualify(package, extension.name), (7, index))
    return comments
