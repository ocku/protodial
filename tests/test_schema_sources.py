from __future__ import annotations

from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool
from grpc_reflection.v1alpha import reflection_pb2

import protodial.schema.export as export_core
from protodial.errors import CliError, SchemaInputError
from protodial.presentation.descriptor import render_descriptor
from protodial.schema.composition import CompositeDescriptorSource
from protodial.schema.export import write_descriptor_set
from protodial.schema.factory import build_descriptor_source
from protodial.schema.local import (
    ProtoDescriptorSource,
    ProtosetDescriptorSource,
    local_descriptor_source,
)
from protodial.schema.reflection import ReflectionDescriptorSource
from protodial.schema.source_protos import (
    field_proto,
    register_file_proto,
    register_pool_file_protos,
)


def _file(
    name: str, package: str, service: str = "API"
) -> descriptor_pb2.FileDescriptorProto:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name=name, package=package, syntax="proto3"
    )
    message = file_proto.message_type.add(name="Message")
    message.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    rpc_service = file_proto.service.add(name=service)
    rpc_service.method.add(
        name="Call",
        input_type=f".{package}.Message",
        output_type=f".{package}.Message",
    )
    return file_proto


def _write_set(path: Path, *files: descriptor_pb2.FileDescriptorProto) -> str:
    path.write_bytes(
        descriptor_pb2.FileDescriptorSet(file=files).SerializeToString()
    )
    return str(path)


def _command(schema: Any) -> Any:
    return SimpleNamespace(schema=schema, timeout=1.0)


def _echo_request(response: Any, request: Any) -> None:
    """Copy a wire-compatible v1 or v1alpha request into an alpha response."""

    response.original_request.ParseFromString(request.SerializeToString())


def test_multiple_proto_entrypoints_compile_with_shared_and_well_known_imports(
    tmp_path: Path,
) -> None:
    (tmp_path / "shared.proto").write_text(
        'syntax = "proto3"; package shared; '
        "message Value { string text = 1; }\n"
    )
    first = tmp_path / "first.proto"
    first.write_text(
        'syntax = "proto3"; package first; import "shared.proto"; '
        "service API { rpc Call(shared.Value) returns (shared.Value); }\n"
    )
    second = tmp_path / "second.proto"
    second.write_text(
        'syntax = "proto3"; package second; '
        'import "google/protobuf/timestamp.proto"; '
        "service API { rpc Call(google.protobuf.Timestamp) "
        "returns (google.protobuf.Timestamp); }\n"
    )
    schema = SimpleNamespace(
        proto=(str(first), str(second)),
        proto_path=(str(tmp_path),),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(_command(schema))

    assert source.list_services() == ("first.API", "second.API")
    assert (
        source.find_symbol("first.API/Call").input_type.full_name
        == "shared.Value"
    )
    assert (
        source.find_symbol("second.API/Call").input_type.full_name
        == "google.protobuf.Timestamp"
    )


def test_proto_entrypoints_with_the_same_basename_remain_distinct(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "api.proto"
    second = second_dir / "api.proto"
    first.write_text(
        'syntax = "proto3"; package first; message Request {} '
        "service API { rpc Call(Request) returns (Request); }\n"
    )
    second.write_text(
        'syntax = "proto3"; package second; message Request {} '
        "service API { rpc Call(Request) returns (Request); }\n"
    )
    schema = SimpleNamespace(
        proto=(str(first), str(second)),
        proto_path=(),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(_command(schema))

    assert source.list_services() == ("first.API", "second.API")
    assert source.find_symbol("first.Request").full_name == "first.Request"
    assert source.find_symbol("second.Request").full_name == "second.Request"


def test_protoset_indexes_files_not_reachable_from_services(
    tmp_path: Path,
) -> None:
    api = _file("api.proto", "api")
    orphan = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan.message_type.add(name="Hidden")
    source = ProtosetDescriptorSource(
        (_write_set(tmp_path / "schema.pb", api, orphan),)
    )

    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"


def test_semantically_invalid_protoset_names_its_input_path(
    tmp_path: Path,
) -> None:
    dependency = _file("dependency.proto", "dependency")
    invalid = _file("invalid.proto", "invalid")
    invalid.dependency.append("missing.proto")
    valid_path = _write_set(tmp_path / "valid.pb", dependency)
    invalid_path = _write_set(tmp_path / "invalid.pb", invalid)

    with pytest.raises(SchemaInputError) as raised:
        ProtosetDescriptorSource((valid_path, invalid_path))

    assert raised.value.path == invalid_path
    assert "invalid.proto" in (raised.value.reason or "")
    assert "missing.proto" in (raised.value.reason or "")


def test_protoset_dependency_may_be_supplied_by_a_later_path(
    tmp_path: Path,
) -> None:
    dependency = _file("dependency.proto", "dependency")
    dependent = _file("dependent.proto", "dependent")
    dependent.dependency.append(dependency.name)
    dependent_path = _write_set(tmp_path / "dependent.pb", dependent)
    dependency_path = _write_set(tmp_path / "dependency.pb", dependency)

    source = ProtosetDescriptorSource((dependent_path, dependency_path))

    assert set(source.file_protos) == {"dependent.proto", "dependency.proto"}


def test_mixed_proto_and_protosets_resolve_dependencies_across_all_sets(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "unrelated.proto"
    unrelated.write_text(
        'syntax = "proto3"; package unrelated; message Message {}\n'
    )
    dependency = _file("dependency.proto", "dependency")
    dependent = _file("dependent.proto", "dependent")
    dependent.dependency.append(dependency.name)
    dependent_path = _write_set(tmp_path / "dependent.pb", dependent)
    dependency_path = _write_set(tmp_path / "dependency.pb", dependency)
    schema = SimpleNamespace(
        proto=(str(unrelated),),
        proto_path=(str(tmp_path),),
        protoset=(dependent_path, dependency_path),
        protoset_out=None,
        use_reflection=False,
    )

    source = local_descriptor_source(_command(schema))

    assert set(source.file_protos) == {
        "unrelated.proto",
        "dependent.proto",
        "dependency.proto",
    }


def test_protoset_conflict_names_descriptor_and_both_origins(
    tmp_path: Path,
) -> None:
    first = _file("duplicate.proto", "one")
    second = _file("duplicate.proto", "two")
    first_path = _write_set(tmp_path / "first.pb", first)
    second_path = _write_set(tmp_path / "second.pb", second)

    with pytest.raises(CliError) as raised:
        ProtosetDescriptorSource((first_path, second_path))

    message = str(raised.value)
    assert "duplicate.proto" in message
    assert first_path in message
    assert second_path in message


def test_descriptors_differing_only_in_source_info_are_accepted(
    tmp_path: Path,
) -> None:
    first_proto = _file("same.proto", "same")
    second_proto = descriptor_pb2.FileDescriptorProto()
    second_proto.CopyFrom(first_proto)
    first_proto.source_code_info.location.add(
        path=(4, 0), leading_comments="first"
    )
    second_proto.source_code_info.location.add(
        path=(4, 0), leading_comments="second"
    )
    first = _write_set(tmp_path / "first.pb", first_proto)
    second = _write_set(tmp_path / "second.pb", second_proto)

    source = ProtosetDescriptorSource((first, second))

    assert source.list_services() == ("same.API",)


def test_identical_duplicate_protoset_files_are_accepted(
    tmp_path: Path,
) -> None:
    file_proto = _file("same.proto", "same")
    first = _write_set(tmp_path / "first.pb", file_proto)
    second = _write_set(tmp_path / "second.pb", file_proto)

    source = ProtosetDescriptorSource((first, second))

    assert source.list_services() == ("same.API",)


class _StatusCode(Enum):
    NOT_FOUND = (5, "not found")
    UNIMPLEMENTED = (12, "unimplemented")


class _RpcError(Exception):
    def code(self) -> _StatusCode:
        return _StatusCode.UNIMPLEMENTED


class _Call:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.used = False

    def __iter__(self) -> _Call:
        return self

    def __next__(self) -> Any:
        if self.used:
            raise StopIteration
        self.used = True
        return self.response

    def cancel(self) -> None:
        pass


def test_reflection_uses_version_specific_wire_classes() -> None:
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                requests.append((path, request.DESCRIPTOR.full_name))
                if ".v1." in path:
                    raise _RpcError()
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.list_services_response.service.add(name="remote.API")
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    assert source.list_services() == ("remote.API",)
    assert requests == [
        (
            "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
            "grpc.reflection.v1.ServerReflectionRequest",
        ),
        (
            "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
            "grpc.reflection.v1alpha.ServerReflectionRequest",
        ),
    ]


def test_reflection_resolves_symbol_outside_advertised_service_graph() -> None:
    api_proto = _file("api.proto", "api")
    orphan_proto = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan_proto.message_type.add(name="Hidden")
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                else:
                    selected = (
                        orphan_proto if value == "orphan.Hidden" else api_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert ("file_containing_symbol", "orphan.Hidden") in requests


def test_exact_reflection_lookup_refreshes_a_primed_suffix_index() -> None:
    api_proto = _file("api.proto", "api")
    orphan_proto = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan_proto.message_type.add(name="Hidden")
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                else:
                    selected = (
                        orphan_proto if value == "orphan.Hidden" else api_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    # Build the suffix index before the unlinked file is known.
    assert source.find_symbol("Message").full_name == "api.Message"
    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"

    # An already-loaded exact lookup neither queries reflection nor invalidates
    # the refreshed index.
    cached_symbols = source._symbols
    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert source._symbols is cached_symbols
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "api.API"),
        ("file_containing_symbol", "orphan.Hidden"),
    ]


def test_suffix_index_refreshes_after_public_pool_loads_a_file() -> None:
    api_proto = _file("api.proto", "api")
    orphan_proto = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan_proto.message_type.add(name="Hidden")
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                elif value == "missing.Unknown":
                    response.error_response.error_code = 5
                    response.error_response.error_message = "not found"
                else:
                    selected = (
                        orphan_proto if value == "orphan.Hidden" else api_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    with pytest.raises(CliError, match="symbol 'Hidden' not found"):
        source.find_symbol("Hidden")
    stale_symbols = source._symbols

    assert (
        source.pool.FindMessageTypeByName("orphan.Hidden").full_name
        == "orphan.Hidden"
    )
    assert source._symbols is stale_symbols
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"

    refreshed_symbols = source._symbols
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    assert source._symbols is refreshed_symbols
    with pytest.raises(CliError, match="symbol 'missing.Unknown' not found"):
        source.find_symbol("missing.Unknown")
    with pytest.raises(CliError, match="symbol 'missing.Unknown' not found"):
        source.find_symbol("missing.Unknown")
    assert source._symbols is refreshed_symbols
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "api.API"),
        ("file_containing_symbol", "orphan.Hidden"),
        ("file_containing_symbol", "missing.Unknown"),
    ]


def test_suffix_index_refreshes_after_public_database_loads_a_file() -> None:
    api_proto = _file("api.proto", "api")
    orphan_proto = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan_proto.message_type.add(name="Hidden")
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                else:
                    selected = (
                        orphan_proto if value == "orphan.proto" else api_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    with pytest.raises(CliError, match="symbol 'Hidden' not found"):
        source.find_symbol("Hidden")
    source.database.FindFileByName("orphan.proto")

    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    cached_symbols = source._symbols
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    assert source._symbols is cached_symbols
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "api.API"),
        ("file_by_filename", "orphan.proto"),
    ]


def test_suffix_index_refreshes_after_extension_lookup_loads_files() -> None:
    api_proto = _file("api.proto", "api")
    base_proto = descriptor_pb2.FileDescriptorProto(
        name="base.proto", package="base", syntax="proto2"
    )
    target = base_proto.message_type.add(name="Target")
    target.extension_range.add(start=100, end=101)
    extension_proto = descriptor_pb2.FileDescriptorProto(
        name="extension.proto",
        package="extra",
        syntax="proto2",
        dependency=("base.proto",),
    )
    extension_proto.extension.add(
        name="note",
        number=100,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".base.Target",
    )
    request_kinds: list[str] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                request_kinds.append(kind)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                elif kind == "file_containing_extension":
                    payload = response.file_descriptor_response
                    payload.file_descriptor_proto.extend(
                        (
                            extension_proto.SerializeToString(),
                            base_proto.SerializeToString(),
                        )
                    )
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        api_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    with pytest.raises(CliError, match="symbol 'note' not found"):
        source.find_symbol("note")
    source.database.FindFileContainingExtension("base.Target", 100)

    assert source.find_symbol("note").full_name == "extra.note"
    cached_symbols = source._symbols
    assert source.find_symbol("note").full_name == "extra.note"
    assert source._symbols is cached_symbols
    assert request_kinds == [
        "list_services",
        "file_containing_symbol",
        "file_containing_extension",
    ]


def test_public_reflection_pool_preserves_source_metadata_for_lazy_files() -> (
    None
):
    docs_proto = descriptor_pb2.FileDescriptorProto(
        name="docs.proto", package="docs", syntax="proto2"
    )
    message_proto = docs_proto.message_type.add(name="Message")
    value_proto = message_proto.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        default_value="from source",
    )
    message_proto.extension_range.add(start=100, end=101)
    docs_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" reflected message\n"
    )
    docs_proto.source_code_info.location.add(
        path=(4, 0, 2, 0), leading_comments=" reflected field\n"
    )

    other_proto = descriptor_pb2.FileDescriptorProto(
        name="other.proto", package="other", syntax="proto3"
    )
    other_proto.message_type.add(name="Message")
    other_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" reflected file route\n"
    )

    extension_proto = descriptor_pb2.FileDescriptorProto(
        name="extension.proto",
        package="extra",
        syntax="proto2",
        dependency=("docs.proto",),
    )
    extension_proto.extension.add(
        name="note",
        number=100,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".docs.Message",
        default_value="extension source",
    )
    extension_proto.source_code_info.location.add(
        path=(7, 0), leading_comments=" reflected extension\n"
    )
    request_kinds: list[str] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                request_kinds.append(kind)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.SetInParent()
                elif kind == "file_containing_symbol":
                    symbol = request.file_containing_symbol
                    selected = (
                        docs_proto if symbol == "docs.Message" else other_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                elif kind == "file_containing_extension":
                    response.file_descriptor_response.file_descriptor_proto.extend(
                        (
                            extension_proto.SerializeToString(),
                            docs_proto.SerializeToString(),
                        )
                    )
                else:
                    raise AssertionError(kind)
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    message = source.pool.FindMessageTypeByName("docs.Message")
    assert render_descriptor(message).startswith(
        "// reflected message\nmessage docs.Message {"
    )
    assert "// reflected field" in render_descriptor(message)
    stored_value = source.file_protos["docs.proto"].message_type[0].field[0]
    assert field_proto(message.fields_by_name["value"]) is stored_value
    assert stored_value.default_value == value_proto.default_value

    other_file = source.pool.FindFileContainingSymbol("other.Message")
    other_message = other_file.message_types_by_name["Message"]
    assert render_descriptor(other_message).startswith(
        "// reflected file route\nmessage other.Message {"
    )

    extension = source.pool.FindExtensionByNumber(message, 100)
    assert render_descriptor(extension).startswith(
        "// reflected extension\nextend .docs.Message {"
    )
    stored_extension = source.file_protos["extension.proto"].extension[0]
    assert field_proto(extension) is stored_extension

    assert source.pool.FindMessageTypeByName("docs.Message") is message
    assert source.pool.FindFileContainingSymbol("other.Message") is other_file
    assert source.pool.FindExtensionByNumber(message, 100) is extension
    assert request_kinds == [
        "list_services",
        "file_containing_symbol",
        "file_containing_symbol",
        "file_containing_extension",
    ]


def test_public_reflection_pool_metadata_is_isolated_by_pool_identity() -> None:
    first_proto = descriptor_pb2.FileDescriptorProto(
        name="same.proto", package="same", syntax="proto3"
    )
    first_proto.message_type.add(name="Message")
    first_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" first reflected pool\n"
    )
    second_proto = descriptor_pb2.FileDescriptorProto()
    second_proto.CopyFrom(first_proto)
    second_proto.source_code_info.location[
        0
    ].leading_comments = " second reflected pool\n"

    def reflected_source(
        file_proto: descriptor_pb2.FileDescriptorProto,
        requests: list[str],
    ) -> ReflectionDescriptorSource:
        class Channel:
            def stream_stream(
                self,
                _path: str,
                *,
                response_deserializer: Any,
                **_kwargs: Any,
            ) -> Any:
                def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                    request = next(request_iterator)
                    kind = request.WhichOneof("message_request")
                    requests.append(kind)
                    response = reflection_pb2.ServerReflectionResponse()
                    _echo_request(response, request)
                    if kind == "list_services":
                        response.list_services_response.SetInParent()
                    else:
                        response.file_descriptor_response.file_descriptor_proto.append(
                            file_proto.SerializeToString()
                        )
                    return _Call(
                        response_deserializer(response.SerializeToString())
                    )

                return rpc

        grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
        return ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    first_requests: list[str] = []
    second_requests: list[str] = []
    first = reflected_source(first_proto, first_requests)
    second = reflected_source(second_proto, second_requests)
    first_message = first.pool.FindMessageTypeByName("same.Message")
    second_message = second.pool.FindMessageTypeByName("same.Message")

    assert render_descriptor(second_message).startswith(
        "// second reflected pool\nmessage same.Message"
    )
    assert render_descriptor(first_message).startswith(
        "// first reflected pool\nmessage same.Message"
    )
    assert render_descriptor(second_message).startswith(
        "// second reflected pool\nmessage same.Message"
    )
    assert first_requests == ["list_services", "file_containing_symbol"]
    assert second_requests == ["list_services", "file_containing_symbol"]


def test_descriptor_source_metadata_override_takes_priority_over_pool() -> None:
    pool_proto = descriptor_pb2.FileDescriptorProto(
        name="priority.proto", package="priority", syntax="proto3"
    )
    pool_proto.message_type.add(name="Message")
    pool_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" pool metadata\n"
    )
    override_proto = descriptor_pb2.FileDescriptorProto()
    override_proto.CopyFrom(pool_proto)
    override_proto.source_code_info.location[
        0
    ].leading_comments = " descriptor metadata\n"
    pool = descriptor_pool.DescriptorPool()
    pool.Add(pool_proto)
    register_pool_file_protos(pool, {pool_proto.name: pool_proto})
    message = pool.FindMessageTypeByName("priority.Message")

    assert render_descriptor(message).startswith(
        "// pool metadata\nmessage priority.Message"
    )
    register_file_proto(message.file, override_proto)

    assert render_descriptor(message).startswith(
        "// descriptor metadata\nmessage priority.Message"
    )


def test_exact_reflection_lookup_registers_metadata_before_indexing() -> None:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="docs.proto", package="docs", syntax="proto3"
    )
    message = file_proto.message_type.add(name="Message")
    message.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    service = file_proto.service.add(name="API")
    service.method.add(
        name="Call",
        input_type=".docs.Message",
        output_type=".docs.Message",
    )
    message_location = file_proto.source_code_info.location.add()
    message_location.path.extend((4, 0))
    message_location.leading_comments = " Exact message documentation.\n"
    field_location = file_proto.source_code_info.location.add()
    field_location.path.extend((4, 0, 2, 0))
    field_location.leading_comments = " Exact field documentation.\n"
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="docs.API")
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        file_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    field = source.find_symbol("docs.Message.value")
    assert source._symbols is None
    assert render_descriptor(field).startswith(
        "// Exact field documentation.\nstring value = 1;"
    )

    message_descriptor = source.find_symbol("docs.Message")
    assert render_descriptor(message_descriptor).startswith(
        "// Exact message documentation.\nmessage docs.Message {"
    )
    assert "// Exact field documentation." in render_descriptor(
        message_descriptor
    )

    assert source.find_symbol("Message").full_name == "docs.Message"
    assert render_descriptor(field).startswith("// Exact field documentation.")
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "docs.Message.value"),
    ]


def test_exact_reflection_lookup_before_index_includes_unlinked_file() -> None:
    api_proto = _file("api.proto", "api")
    orphan_proto = descriptor_pb2.FileDescriptorProto(
        name="orphan.proto", package="orphan", syntax="proto3"
    )
    orphan_proto.message_type.add(name="Hidden")
    requests: list[tuple[str, str]] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                value = getattr(request, kind)
                requests.append((kind, value))
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.service.add(name="api.API")
                else:
                    selected = (
                        orphan_proto if value == "orphan.Hidden" else api_proto
                    )
                    response.file_descriptor_response.file_descriptor_proto.append(
                        selected.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    source = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)

    assert source.find_symbol("orphan.Hidden").full_name == "orphan.Hidden"
    assert source.find_symbol("Hidden").full_name == "orphan.Hidden"
    assert requests == [
        ("list_services", ""),
        ("file_containing_symbol", "orphan.Hidden"),
        ("file_containing_symbol", "api.API"),
    ]


def test_composite_source_resolves_local_and_reflection_symbols(
    tmp_path: Path,
) -> None:
    local_proto = _file("local.proto", "local")
    remote_proto = _file("remote.proto", "remote")
    local = ProtosetDescriptorSource(
        (_write_set(tmp_path / "local.pb", local_proto),)
    )

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if request.WhichOneof("message_request") == "list_services":
                    response.list_services_response.service.add(
                        name="remote.API"
                    )
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        remote_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    reflection = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)
    source = CompositeDescriptorSource(local, reflection)

    assert source.list_services() == ("local.API", "remote.API")
    assert source.find_symbol("local.API/Call").full_name == "local.API.Call"
    assert source.find_symbol("remote.API/Call").full_name == "remote.API.Call"


def test_public_composite_pool_preserves_lazy_reflection_source_metadata(
    tmp_path: Path,
) -> None:
    local = ProtosetDescriptorSource(
        (_write_set(tmp_path / "local.pb", _file("local.proto", "local")),)
    )
    remote_proto = descriptor_pb2.FileDescriptorProto(
        name="remote.proto", package="remote", syntax="proto3"
    )
    remote_message = remote_proto.message_type.add(name="Message")
    remote_message.field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    remote_proto.source_code_info.location.add(
        path=(4, 0), leading_comments=" composite reflection\n"
    )
    remote_proto.source_code_info.location.add(
        path=(4, 0, 2, 0), leading_comments=" composite field\n"
    )
    request_kinds: list[str] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                request_kinds.append(kind)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.SetInParent()
                else:
                    response.file_descriptor_response.file_descriptor_proto.append(
                        remote_proto.SerializeToString()
                    )
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    grpc = SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)
    reflection = ReflectionDescriptorSource(Channel(), [], 1.0, grpc)
    source = CompositeDescriptorSource(local, reflection)

    message = source.pool.FindMessageTypeByName("remote.Message")

    assert render_descriptor(message).startswith(
        "// composite reflection\nmessage remote.Message {"
    )
    assert "// composite field" in render_descriptor(message)
    stored_field = source.file_protos["remote.proto"].message_type[0].field[0]
    assert field_proto(message.fields_by_name["value"]) is stored_field
    assert source.pool.FindMessageTypeByName("remote.Message") is message
    assert request_kinds == ["list_services", "file_containing_symbol"]


def test_common_builder_defaults_local_sources_to_no_reflection(
    tmp_path: Path,
) -> None:
    schema_path = _write_set(
        tmp_path / "local.pb", _file("local.proto", "local")
    )
    schema = SimpleNamespace(
        proto=(),
        proto_path=(),
        protoset=(schema_path,),
        protoset_out=None,
        use_reflection=False,
    )

    source = build_descriptor_source(_command(schema), SimpleNamespace())

    assert isinstance(source, ProtosetDescriptorSource)
    assert source.list_services() == ("local.API",)


def test_descriptor_set_export_is_complete_dependency_ordered_and_atomic(
    tmp_path: Path,
) -> None:
    dependency = descriptor_pb2.FileDescriptorProto(
        name="dependency.proto", package="dependency", syntax="proto3"
    )
    dependency.message_type.add(name="Message")
    api = descriptor_pb2.FileDescriptorProto(
        name="api.proto", package="api", syntax="proto3"
    )
    api.dependency.append("dependency.proto")
    service = api.service.add(name="API")
    service.method.add(
        name="Call",
        input_type=".dependency.Message",
        output_type=".dependency.Message",
    )
    source_path = _write_set(tmp_path / "input.pb", api, dependency)
    source = ProtosetDescriptorSource((source_path,))
    output = tmp_path / "nested" / "schema.pb"

    write_descriptor_set(source, output)

    exported = descriptor_pb2.FileDescriptorSet.FromString(output.read_bytes())
    assert [file_proto.name for file_proto in exported.file] == [
        "dependency.proto",
        "api.proto",
    ]
    assert list(output.parent.glob(f".{output.name}.*")) == []


def test_descriptor_export_preserves_primary_when_temporary_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ProtosetDescriptorSource(
        (_write_set(tmp_path / "input.pb", _file("api.proto", "api")),)
    )
    output = tmp_path / "schema.pb"
    primary = OSError("replace primary")
    real_unlink = Path.unlink

    def fail_replace(_source: object, _destination: object) -> None:
        raise primary

    def fail_temporary_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(f".{output.name}."):
            raise PermissionError("unlink cleanup")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(export_core.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)

    with pytest.raises(OSError, match="replace primary") as raised:
        write_descriptor_set(source, output)

    assert raised.value is primary
    assert getattr(primary, "_protodial_notes") == (
        "descriptor temporary-file cleanup also failed: unlink cleanup",
    )
