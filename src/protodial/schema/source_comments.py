"""Recover declaration comments from retained protobuf SourceCodeInfo."""

from __future__ import annotations

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FileDescriptor

from .source_protos import source_file_proto


def comments_for_file(file_descriptor: FileDescriptor) -> dict[str, str]:
    """Map qualified declaration names to their retained source comments."""
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
