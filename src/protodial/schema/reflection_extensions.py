"""Index extension declarations retained by reflection descriptors."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from google.protobuf import descriptor_pb2


def _extensions_in_message(
    message: descriptor_pb2.DescriptorProto,
) -> Sequence[descriptor_pb2.FieldDescriptorProto]:
    extensions = list(message.extension)
    for nested in message.nested_type:
        extensions.extend(_extensions_in_message(nested))
    return extensions


def _file_extensions(
    file_proto: descriptor_pb2.FileDescriptorProto,
) -> Sequence[descriptor_pb2.FieldDescriptorProto]:
    extensions = list(file_proto.extension)
    for message in file_proto.message_type:
        extensions.extend(_extensions_in_message(message))
    return extensions


def find_extension_file(
    file_protos: Iterable[descriptor_pb2.FileDescriptorProto],
    extendee_name: str,
    extension_number: int,
) -> descriptor_pb2.FileDescriptorProto | None:
    """Find the retained file declaring a numbered extension."""
    for file_proto in file_protos:
        if any(
            extension.extendee.lstrip(".") == extendee_name
            and extension.number == extension_number
            for extension in _file_extensions(file_proto)
        ):
            return file_proto
    return None


def known_extension_numbers(
    file_protos: Iterable[descriptor_pb2.FileDescriptorProto],
    extendee_name: str,
) -> set[int]:
    """Return all retained extension numbers for a message type."""
    return {
        extension.number
        for file_proto in file_protos
        for extension in _file_extensions(file_proto)
        if extension.extendee.lstrip(".") == extendee_name
    }
