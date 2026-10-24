from __future__ import annotations

from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf import (
    any_pb2,
    descriptor_pb2,
    descriptor_pool,
    json_format,
    message_factory,
    timestamp_pb2,
)
from google.protobuf.descriptor import (
    Descriptor,
    MethodDescriptor,
    ServiceDescriptor,
)

from protodial.argument_parser import parse_arguments
from protodial.errors import CliError
from protodial.presentation.descriptor import render_descriptor
from protodial.presentation.templates import message_template
from protodial.rpc import describe, list_symbols
from protodial.schema.local import protoset_descriptor_source
from protodial.schema.reflection import ReflectionDescriptorSource


def _field(
    message: descriptor_pb2.DescriptorProto,
    name: str,
    number: int,
    field_type: int,
    *,
    label: int = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
    type_name: str = "",
) -> descriptor_pb2.FieldDescriptorProto:
    field = message.field.add(
        name=name, number=number, type=field_type, label=label
    )
    if type_name:
        field.type_name = type_name
    return field


def _fixture_set() -> descriptor_pb2.FileDescriptorSet:
    alpha = descriptor_pb2.FileDescriptorProto(
        name="alpha.proto", package="alpha", syntax="proto2"
    )
    request = alpha.message_type.add(name="Request")
    request.reserved_range.add(start=20, end=23)
    request.reserved_name.append("old_name")
    request.extension_range.add(start=100, end=536_870_912)

    nested = request.nested_type.add(name="Nested")
    _field(nested, "value", 1, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)
    state = request.enum_type.add(name="State")
    state.value.add(name="UNKNOWN", number=0)
    state.value.add(name="READY", number=1)
    state.reserved_range.add(start=3, end=5)

    details = request.nested_type.add(name="Details")
    _field(details, "note", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)

    entry = request.nested_type.add(name="LabelsEntry")
    entry.options.map_entry = True
    _field(entry, "key", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    _field(entry, "value", 2, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)

    _field(
        request,
        "id",
        1,
        descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REQUIRED,
    )
    _field(
        request,
        "tags",
        2,
        descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
    )
    _field(
        request,
        "labels",
        3,
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type_name=".alpha.Request.LabelsEntry",
    )
    request.oneof_decl.add(name="selection")
    choice_name = _field(
        request,
        "choice_name",
        4,
        descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    choice_name.oneof_index = 0
    choice_number = _field(
        request,
        "choice_number",
        5,
        descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    )
    choice_number.oneof_index = 0
    _field(
        request,
        "nested",
        6,
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".alpha.Request.Nested",
    )
    mode = _field(
        request,
        "state",
        7,
        descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        type_name=".alpha.Request.State",
    )
    mode.default_value = "READY"
    raw = _field(
        request, "raw", 8, descriptor_pb2.FieldDescriptorProto.TYPE_BYTES
    )
    raw.default_value = r"\001\377"
    _field(
        request,
        "details",
        9,
        descriptor_pb2.FieldDescriptorProto.TYPE_GROUP,
        type_name=".alpha.Request.Details",
    )

    extension = alpha.extension.add(
        name="request_note",
        number=100,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".alpha.Request",
    )
    extension.options.deprecated = True

    service = alpha.service.add(name="TestService")
    service.options.deprecated = True
    cardinalities = (
        ("Unary", False, False),
        ("Upload", True, False),
        ("Watch", False, True),
        ("Chat", True, True),
    )
    for name, client_streaming, server_streaming in cardinalities:
        method = service.method.add(
            name=name,
            input_type=".alpha.Request",
            output_type=".alpha.Request",
            client_streaming=client_streaming,
            server_streaming=server_streaming,
        )
        if name == "Unary":
            method.options.deprecated = True

    # Leading comments are keyed by descriptor source paths.
    location = alpha.source_code_info.location.add()
    location.path.extend((4, 0))
    location.leading_comments = " Request documentation.\n"
    field_location = alpha.source_code_info.location.add()
    field_location.path.extend((4, 0, 2, 0))
    field_location.leading_comments = " Stable request identifier.\n"

    beta = descriptor_pb2.FileDescriptorProto(
        name="beta.proto", package="beta", syntax="proto3"
    )
    beta_request = beta.message_type.add(name="Request")
    _field(
        beta_request, "name", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    )
    beta_service = beta.service.add(name="TestService")
    beta_service.method.add(
        name="Unary", input_type=".beta.Request", output_type=".beta.Request"
    )
    return descriptor_pb2.FileDescriptorSet(file=(alpha, beta))


@pytest.fixture
def protoset_path(tmp_path: Path) -> str:
    path = tmp_path / "schema.protoset"
    path.write_bytes(_fixture_set().SerializeToString())
    return str(path)


def test_service_listing_is_sorted_and_methods_keep_declaration_order(
    protoset_path: str,
) -> None:
    source = protoset_descriptor_source((protoset_path,))

    assert source.list_services() == ("alpha.TestService", "beta.TestService")
    service = source.find_symbol("alpha.TestService")
    assert isinstance(service, ServiceDescriptor)
    assert [method.name for method in service.methods] == [
        "Unary",
        "Upload",
        "Watch",
        "Chat",
    ]


def test_symbol_resolution_supports_suffixes_and_both_method_notations(
    protoset_path: str,
) -> None:
    source = protoset_descriptor_source((protoset_path,))

    assert (
        source.find_symbol("alpha.Request.Nested").full_name
        == "alpha.Request.Nested"
    )
    assert source.find_symbol("Nested").full_name == "alpha.Request.Nested"
    assert (
        source.find_symbol("alpha.TestService/Chat").full_name
        == "alpha.TestService.Chat"
    )
    assert (
        source.find_symbol("TestService/Chat").full_name
        == "alpha.TestService.Chat"
    )
    assert isinstance(
        source.find_symbol("alpha.TestService.Unary"), MethodDescriptor
    )
    assert source.find_symbol("request_note").full_name == "alpha.request_note"


def test_symbol_resolution_reports_ambiguity_and_nearby_names(
    protoset_path: str,
) -> None:
    source = protoset_descriptor_source((protoset_path,))

    with pytest.raises(
        CliError,
        match=r"ambiguous symbol 'Request'.*alpha.Request.*beta.Request",
    ):
        source.find_symbol("Request")
    with pytest.raises(CliError, match=r"not found; nearby: alpha.Request"):
        source.find_symbol("alpha.Reques")


def test_renderer_handles_messages_comments_maps_oneofs_ranges_and_extensions(
    protoset_path: str,
) -> None:
    source = protoset_descriptor_source((protoset_path,))
    request = source.find_symbol("alpha.Request")
    rendered = render_descriptor(request)

    assert rendered.startswith(
        "// Request documentation.\nmessage alpha.Request {"
    )
    assert 'reserved "old_name";' in rendered
    assert "reserved 20 to 22;" in rendered
    assert "extensions 100 to max;" in rendered
    assert "message Nested {" in rendered
    assert "enum State {" in rendered
    assert "reserved 3 to 5;" in rendered
    assert "required string id = 1;" in rendered
    assert "// Stable request identifier." in rendered
    assert "repeated string tags = 2;" in rendered
    assert "map<string, int32> labels = 3;" in rendered
    assert "message LabelsEntry" not in rendered
    assert "oneof selection {" in rendered
    assert ".alpha.Request.State state = 7 [default = READY];" in rendered
    assert r'optional bytes raw = 8 [default = "\001\377"];' in rendered
    assert "optional group Details = 9 {" in rendered
    assert "optional string note = 1;" in rendered
    assert "message Details {" not in rendered

    extension = render_descriptor(source.find_symbol("alpha.request_note"))
    assert "extend .alpha.Request" in extension
    assert (
        "optional string request_note = 100 [deprecated = true];" in extension
    )


def test_renderer_handles_all_rpc_cardinalities(protoset_path: str) -> None:
    source = protoset_descriptor_source((protoset_path,))
    rendered = render_descriptor(source.find_symbol("alpha.TestService"))

    assert rendered.splitlines()[:2] == [
        "service alpha.TestService {",
        "  option deprecated = true;",
    ]
    assert (
        "rpc Unary (.alpha.Request) returns (.alpha.Request) "
        "[deprecated = true];" in rendered
    )
    assert (
        "rpc Upload (stream .alpha.Request) returns (.alpha.Request);"
        in rendered
    )
    assert (
        "rpc Watch (.alpha.Request) returns (stream .alpha.Request);"
        in rendered
    )
    assert (
        "rpc Chat (stream .alpha.Request) returns (stream .alpha.Request);"
        in rendered
    )


def test_message_template_is_recursive_but_terminates(
    protoset_path: str,
) -> None:
    source = protoset_descriptor_source((protoset_path,))
    request = source.find_symbol("alpha.Request")
    assert isinstance(request, Descriptor)

    rendered = message_template(request)
    assert "\n" not in rendered
    assert '"id": ""' in rendered
    assert '"tags": [' in rendered
    assert '"labels": {' in rendered
    assert '"nested": {' in rendered
    assert '"choiceName": ""' in rendered
    assert "choiceNumber" not in rendered
    assert '"state": "UNKNOWN"' in rendered


def test_nested_well_known_types_stay_valid_at_template_depth_limit() -> None:
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(timestamp_pb2.DESCRIPTOR.serialized_pb)
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="template_depth.proto",
        package="template_depth",
        syntax="proto3",
        dependency=("google/protobuf/timestamp.proto",),
    )
    message = file_proto.message_type.add(name="Request")
    _field(
        message,
        "created_at",
        1,
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".google.protobuf.Timestamp",
    )
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("template_depth.Request")
    message_class = message_factory.GetMessageClass(descriptor)

    rendered = message_template(descriptor, max_depth=1)

    assert rendered == '{"createdAt": "1970-01-01T00:00:00Z"}'
    parsed = json_format.Parse(rendered, message_class())
    assert parsed.HasField("created_at")


def test_message_template_for_any_is_valid_proto_json() -> None:
    rendered = message_template(any_pb2.Any.DESCRIPTOR)

    assert rendered == "{}"
    assert json_format.Parse(rendered, any_pb2.Any()) == any_pb2.Any()


def test_protoset_discovery_does_not_open_a_channel(
    protoset_path: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    grpc = SimpleNamespace()
    list_command = parse_arguments(
        ["list", "--protoset", protoset_path, "unused.example:443"]
    )
    list_symbols(list_command, grpc)
    assert capsys.readouterr().out == "alpha.TestService\nbeta.TestService\n"

    describe_command = parse_arguments(
        [
            "describe",
            "--protoset",
            protoset_path,
            "--template",
            "unused.example:443",
            "alpha.Request",
        ]
    )
    describe(describe_command, grpc)
    assert '"id": ""' in capsys.readouterr().out

    method_template = parse_arguments(
        [
            "describe",
            "--protoset",
            protoset_path,
            "--template",
            "grpc://unused.example:443/alpha.TestService/Unary",
        ]
    )
    describe(method_template, grpc)
    assert '"id": ""' in capsys.readouterr().out

    method_description = parse_arguments(
        [
            "describe",
            "--protoset",
            protoset_path,
            "grpc://unused.example:443/alpha.TestService/Unary",
        ]
    )
    describe(method_description, grpc)
    assert capsys.readouterr().out.startswith(
        "rpc Unary (.alpha.Request) returns (.alpha.Request)"
    )


def test_reflection_source_caches_service_list_and_uses_bounded_requests() -> (
    None
):
    from grpc_reflection.v1alpha import reflection_pb2

    descriptor_set = _fixture_set()
    alpha = descriptor_set.file[0]
    requests: list[str] = []

    class StatusCode(Enum):
        NOT_FOUND = (5, "not found")
        UNIMPLEMENTED = (12, "unimplemented")

    class Call:
        def __init__(self, response: object) -> None:
            self.response = response
            self.used = False

        def __iter__(self) -> Call:
            return self

        def __next__(self) -> object:
            if self.used:
                raise StopIteration
            self.used = True
            return self.response

        def cancel(self) -> None:
            pass

    class Channel:
        def stream_stream(self, _path: str, **_kwargs: object) -> object:
            def rpc(request_iterator: object, **_call_kwargs: object) -> Call:
                request = next(request_iterator)  # type: ignore[arg-type]
                kind = request.WhichOneof("message_request")
                requests.append(kind)
                if kind == "list_services":
                    response = reflection_pb2.ServerReflectionResponse()
                    response.list_services_response.service.add(
                        name="alpha.TestService"
                    )
                else:
                    response = reflection_pb2.ServerReflectionResponse()
                    response.file_descriptor_response.file_descriptor_proto.append(
                        alpha.SerializeToString()
                    )
                response.original_request.ParseFromString(
                    request.SerializeToString()
                )
                return Call(response)

            return rpc

    grpc = SimpleNamespace(StatusCode=StatusCode, RpcError=Exception)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    assert source.list_services() == ("alpha.TestService",)
    assert source.list_services() == ("alpha.TestService",)
    assert (
        source.find_symbol("Request.Nested").full_name == "alpha.Request.Nested"
    )
    with pytest.raises(CliError, match="not found"):
        source.find_symbol("Missing")
    assert requests == ["list_services", "file_containing_symbol"]


def test_proto_source_preserves_optional_and_source_comments(
    tmp_path: Path,
) -> None:
    proto = tmp_path / "fixture.proto"
    proto.write_text(
        'syntax = "proto3";\n'
        "package local;\n"
        "// Input docs.\n"
        "message Input { optional string value = 1; }\n"
        "service API { rpc Call(Input) returns (Input); }\n"
    )
    command = parse_arguments(
        ["describe", "--proto", str(proto), "unused.example:443", "local.Input"]
    )
    from protodial.schema.local import local_descriptor_source

    rendered = render_descriptor(
        local_descriptor_source(command).find_symbol("local.Input")
    )
    assert rendered.startswith("// Input docs.\nmessage local.Input {")
    assert "optional string value = 1;" in rendered
