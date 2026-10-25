"""Descriptor-backed method, symbol, field, and value candidates."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import cast

from google.protobuf.descriptor import (
    Descriptor,
    EnumDescriptor,
    FieldDescriptor,
    FileDescriptor,
    MethodDescriptor,
    ServiceDescriptor,
)

from ..schema.contracts import DescriptorSource
from ._arguments import Candidate, Scan, unique_candidates
from ._saved_protos import SavedProtoSource
from ._selection import selected_method

_SCALAR_TYPES = {
    FieldDescriptor.TYPE_DOUBLE: "double",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int64",
    FieldDescriptor.TYPE_UINT64: "uint64",
    FieldDescriptor.TYPE_INT32: "int32",
    FieldDescriptor.TYPE_FIXED64: "fixed64",
    FieldDescriptor.TYPE_FIXED32: "fixed32",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "uint32",
    FieldDescriptor.TYPE_SFIXED32: "sfixed32",
    FieldDescriptor.TYPE_SFIXED64: "sfixed64",
    FieldDescriptor.TYPE_SINT32: "sint32",
    FieldDescriptor.TYPE_SINT64: "sint64",
}


def method_candidates(
    sources: Iterable[DescriptorSource], prefix: str = ""
) -> list[Candidate]:
    result: list[Candidate] = []
    for source in sources:
        if isinstance(source, SavedProtoSource):
            result.extend(source.method_candidates(prefix))
            continue
        try:
            services = source.list_services()
        except Exception:
            continue
        for service_name in services:
            deadline = getattr(source, "_protodial_completion_deadline", None)
            if (
                isinstance(deadline, (int, float))
                and time.monotonic() >= deadline
            ):
                break
            try:
                service = source.find_symbol(service_name)
            except Exception:
                continue
            if not isinstance(service, ServiceDescriptor):
                continue
            for method in service.methods:
                cardinality = (
                    ("stream" if method.client_streaming else "unary")
                    + " request, "
                    + ("stream" if method.server_streaming else "unary")
                    + " response"
                )
                result.append(
                    Candidate(
                        f"{service.full_name}/{method.name}",
                        cardinality,
                    )
                )
    return unique_candidates(result)


def symbol_candidates(
    sources: Iterable[DescriptorSource], prefix: str = ""
) -> list[Candidate]:
    result: list[Candidate] = []
    seen_messages: set[str] = set()
    seen_enums: set[str] = set()
    seen_files: set[tuple[int, str]] = set()

    def add_enum(enum: EnumDescriptor) -> None:
        if enum.full_name in seen_enums:
            return
        seen_enums.add(enum.full_name)
        result.append(Candidate(enum.full_name, "enum"))
        value_scope = (
            enum.containing_type.full_name
            if enum.containing_type is not None
            else enum.file.package
        )
        result.extend(
            Candidate(
                f"{value_scope}.{value.name}" if value_scope else value.name,
                "enum value",
            )
            for value in enum.values
        )

    def add_message(message: Descriptor) -> None:
        if message.full_name in seen_messages:
            return
        seen_messages.add(message.full_name)
        result.append(Candidate(message.full_name, "message"))
        result.extend(
            Candidate(field.full_name, "field") for field in message.fields
        )
        result.extend(
            Candidate(oneof.full_name, "oneof") for oneof in message.oneofs
        )
        result.extend(
            Candidate(extension.full_name, "extension")
            for extension in message.extensions
        )
        for enum in message.enum_types:
            add_enum(enum)
        for nested in message.nested_types:
            if not nested.GetOptions().map_entry:
                add_message(nested)

    def add_service(service: ServiceDescriptor) -> None:
        result.append(Candidate(service.full_name, "service"))
        result.extend(
            Candidate(f"{service.full_name}/{method.name}", "RPC method")
            for method in service.methods
        )

    def add_file(file_descriptor: FileDescriptor, source_id: int) -> None:
        key = (source_id, file_descriptor.name)
        if key in seen_files:
            return
        seen_files.add(key)
        for dependency in file_descriptor.dependencies:
            add_file(dependency, source_id)
        for service in file_descriptor.services_by_name.values():
            add_service(service)
        for message in file_descriptor.message_types_by_name.values():
            add_message(message)
        for enum in file_descriptor.enum_types_by_name.values():
            add_enum(enum)
        result.extend(
            Candidate(extension.full_name, "extension")
            for extension in file_descriptor.extensions_by_name.values()
        )

    for source in sources:
        if isinstance(source, SavedProtoSource):
            result.extend(source.symbol_candidates(prefix))
            continue
        source_id = id(source.pool)
        file_protos = getattr(source, "file_protos", None)
        if isinstance(file_protos, Mapping):
            typed_file_protos = cast(Mapping[object, object], file_protos)
            traversed_file = False
            for file_name in sorted(
                name for name in typed_file_protos if isinstance(name, str)
            ):
                try:
                    add_file(source.pool.FindFileByName(file_name), source_id)
                    traversed_file = True
                except KeyError:
                    continue
            if traversed_file:
                continue
        try:
            services = source.list_services()
        except Exception:
            continue
        for service_name in services:
            deadline = getattr(source, "_protodial_completion_deadline", None)
            if (
                isinstance(deadline, (int, float))
                and time.monotonic() >= deadline
            ):
                break
            try:
                service = source.find_symbol(service_name)
            except Exception:
                continue
            if not isinstance(service, ServiceDescriptor):
                continue
            add_file(service.file, source_id)
    return unique_candidates(result)


def _method_descriptor(
    sources: Iterable[DescriptorSource], method_name: str | None
) -> MethodDescriptor | None:
    if not method_name:
        return None
    for source in sources:
        try:
            symbol = source.find_symbol(method_name)
        except Exception:
            continue
        if isinstance(symbol, MethodDescriptor):
            return symbol
    return None


def _field_type(field: FieldDescriptor) -> str:
    if field.message_type is not None:
        name = field.message_type.full_name
    elif field.enum_type is not None:
        name = field.enum_type.full_name
    else:
        name = _SCALAR_TYPES.get(field.type, "scalar")
    if field.is_repeated:
        return f"repeated {name}"
    return name


def _field_values(field: FieldDescriptor) -> list[Candidate]:
    if field.enum_type is not None:
        return [
            Candidate(
                f'["{value.name}"]' if field.is_repeated else value.name,
                "repeated enum JSON array"
                if field.is_repeated
                else "enum value",
            )
            for value in field.enum_type.values
        ]
    if field.type == FieldDescriptor.TYPE_BOOL:
        if field.is_repeated:
            return [
                Candidate("[false]", "repeated boolean JSON array"),
                Candidate("[true]", "repeated boolean JSON array"),
            ]
        return [Candidate("false", "boolean"), Candidate("true", "boolean")]
    return []


def field_candidates(
    scan: Scan,
    kind: str,
    prefix: str,
    sources: Iterable[DescriptorSource],
) -> list[Candidate]:
    method = _method_descriptor(sources, selected_method(scan))
    if method is None:
        return []
    message = (
        method.output_type if kind == "output_field" else method.input_type
    )
    if kind in {"input_field", "output_field", "field_file"}:
        fields = [
            field
            for field in message.fields
            if field.type == FieldDescriptor.TYPE_BYTES
            and not field.is_repeated
        ]
    else:
        fields = list(message.fields)

    if kind == "field" and "=" in prefix:
        name, _, value_prefix = prefix.partition("=")
        field = next(
            (item for item in fields if name in {item.name, item.json_name}),
            None,
        )
        if field is None:
            return []
        return [
            Candidate(f"{name}={item.value}", item.description)
            for item in _field_values(field)
            if item.value.startswith(value_prefix)
        ]

    result: list[Candidate] = []
    for field in fields:
        value = field.json_name
        if kind in {"field", "field_file"}:
            value += "="
        if value.startswith(prefix):
            result.append(
                Candidate(
                    value,
                    _field_type(field),
                    append_space=not value.endswith("="),
                )
            )
    return unique_candidates(result)
