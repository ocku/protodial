from __future__ import annotations

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import OneofDescriptor

from protodial.presentation.descriptor import render_descriptor
from protodial.schema.registry import (
    source_from_file_protos,
)


def test_source_comments_survive_more_than_256_registered_files() -> None:
    files: list[descriptor_pb2.FileDescriptorProto] = []
    for index in range(257):
        file_proto = descriptor_pb2.FileDescriptorProto(
            name=f"file_{index}.proto", package=f"edge{index}", syntax="proto3"
        )
        file_proto.message_type.add(name="Message")
        file_proto.source_code_info.location.add(
            path=(4, 0), leading_comments=f" comment {index}\n"
        )
        files.append(file_proto)

    source = source_from_file_protos(files)

    assert render_descriptor(source.find_symbol("edge0.Message")).startswith(
        "// comment 0\nmessage edge0.Message"
    )


def test_source_comments_are_isolated_by_descriptor_source_identity() -> None:
    first_proto = descriptor_pb2.FileDescriptorProto(
        name="same.proto", package="same", syntax="proto3"
    )
    first_proto.message_type.add(name="Message")
    first_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" first source\n"
    )
    second_proto = descriptor_pb2.FileDescriptorProto()
    second_proto.CopyFrom(first_proto)
    second_proto.source_code_info.location[
        0
    ].leading_comments = " second source\n"

    first = source_from_file_protos((first_proto,))
    second = source_from_file_protos((second_proto,))
    first_message = first.find_symbol("same.Message")
    second_message = second.find_symbol("same.Message")

    assert render_descriptor(second_message).startswith(
        "// second source\nmessage same.Message"
    )
    assert render_descriptor(first_message).startswith(
        "// first source\nmessage same.Message"
    )
    assert render_descriptor(second.find_symbol("same.Message")).startswith(
        "// second source\nmessage same.Message"
    )
    assert render_descriptor(first.find_symbol("same.Message")).startswith(
        "// first source\nmessage same.Message"
    )


def test_oneof_symbols_are_resolvable_and_renderable() -> None:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="oneof.proto", package="edge", syntax="proto3"
    )
    message = file_proto.message_type.add(name="Container")
    message.oneof_decl.add(name="choice")
    message.field.add(
        name="text",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        oneof_index=0,
    )
    source = source_from_file_protos((file_proto,))

    symbol = source.find_symbol("edge.Container.choice")

    assert isinstance(symbol, OneofDescriptor)
    assert render_descriptor(symbol) == "oneof choice {\n  string text = 1;\n}"
