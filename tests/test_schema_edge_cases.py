from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest
from google.protobuf import descriptor_database, descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import OneofDescriptor
from grpc_reflection.v1alpha import reflection_pb2

from protodial.argument_parser import parse_arguments
from protodial.errors import CliError
from protodial.presentation.descriptor import render_descriptor
from protodial.schema.export import descriptor_set
from protodial.schema.local import (
    ProtoDescriptorSource,
    _common_entrypoint_root,
    _compile_proto_registry,
    _protoc_arguments,
    _ProtoCompilerInputs,
)
from protodial.schema.reflection import (
    ReflectionDescriptorSource,
    reflection_database,
)
from protodial.schema.registry import (
    IndexedDescriptorSource,
    source_from_file_protos,
)


class _StatusCode(Enum):
    NOT_FOUND = (5, "not found")
    UNIMPLEMENTED = (12, "unimplemented")


class _RpcError(Exception):
    def code(self) -> _StatusCode:
        return _StatusCode.UNIMPLEMENTED


class _Call:
    def __init__(self, response: Any) -> None:
        self._response = response
        self._used = False

    def __iter__(self) -> _Call:
        return self

    def __next__(self) -> Any:
        if self._used:
            raise StopIteration
        self._used = True
        return self._response

    def cancel(self) -> None:
        pass


def _grpc() -> Any:
    return SimpleNamespace(StatusCode=_StatusCode, RpcError=_RpcError)


def _echo_request(response: Any, request: Any) -> None:
    response.original_request.ParseFromString(request.SerializeToString())


class _DescriptorBatchChannel:
    def __init__(self, *batches: tuple[bytes, ...]) -> None:
        self.batches = list(batches)

    def stream_stream(
        self, _path: str, *, response_deserializer: Any, **_kwargs: Any
    ) -> Any:
        def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
            request = next(requests)
            response = reflection_pb2.ServerReflectionResponse()
            _echo_request(response, request)
            response.file_descriptor_response.file_descriptor_proto.extend(
                self.batches.pop(0)
            )
            return _Call(response_deserializer(response.SerializeToString()))

        return rpc


def test_protoc_entrypoints_use_forward_slashes_on_windows() -> None:
    root = PureWindowsPath("C:/schemas")
    descriptor_path = PureWindowsPath("C:/output/schema.pb")
    relative_files = (
        PureWindowsPath("nested/types.proto"),
        PureWindowsPath("first/service.proto"),
        PureWindowsPath("second/service.proto"),
    )
    inputs = _ProtoCompilerInputs(
        files=tuple(root / path for path in relative_files),
        roots=(root,),
        relative_files=relative_files,
    )

    arguments = _protoc_arguments(descriptor_path, inputs)

    assert arguments[3:] == [
        f"-I{root}",
        f"--descriptor_set_out={descriptor_path}",
        "--include_imports",
        "--include_source_info",
        "nested/types.proto",
        "first/service.proto",
        "second/service.proto",
    ]


def test_nested_proto_entrypoints_are_independent_of_argument_order(
    tmp_path: Path,
) -> None:
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    nested = nested_dir / "types.proto"
    nested.write_text('syntax = "proto3"; package nested; message Request {}\n')
    api = tmp_path / "api.proto"
    api.write_text(
        'syntax = "proto3"; package api; import "nested/types.proto"; '
        "service API { rpc Call(nested.Request) returns (nested.Request); }\n"
    )
    schema = SimpleNamespace(
        proto=(str(nested), str(api)),
        proto_path=(),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(SimpleNamespace(schema=schema, timeout=1.0))

    assert source.find_symbol("nested.Request").full_name == "nested.Request"
    assert (
        source.find_symbol("api.API/Call").input_type.full_name
        == "nested.Request"
    )


def test_concurrent_proto_compilation_is_process_isolated(
    tmp_path: Path,
) -> None:
    commands = []
    for index in range(6):
        proto = tmp_path / f"schema-{index}.proto"
        proto.write_text(
            f'syntax = "proto3"; package p{index}; service S {{}}\n',
            encoding="utf-8",
        )
        commands.append(
            parse_arguments(["list", "--proto", str(proto), "unused.invalid"])
        )

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        registries = tuple(executor.map(_compile_proto_registry, commands))

    assert [tuple(registry.files) for registry in registries] == [
        (f"schema-{index}.proto",) for index in range(6)
    ]


def test_explicit_proto_is_not_shadowed_by_an_earlier_import_root(
    tmp_path: Path,
) -> None:
    default_root = tmp_path / "default"
    project_root = tmp_path / "project"
    default_root.mkdir()
    project_root.mkdir()
    (default_root / "service.proto").write_text(
        'syntax = "proto3"; package shadowed; service Wrong {}\n'
    )
    requested = project_root / "service.proto"
    requested.write_text(
        'syntax = "proto3"; package intended; service Right {}\n'
    )
    schema = SimpleNamespace(
        proto=(str(requested),),
        proto_path=(str(default_root),),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(SimpleNamespace(schema=schema, timeout=1.0))

    assert source.list_services() == ("intended.Right",)


@pytest.mark.parametrize("reverse", [False, True])
def test_same_named_proto_entrypoints_are_independent_of_argument_order(
    tmp_path: Path, reverse: bool
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "service.proto"
    second = second_root / "service.proto"
    first.write_text('syntax = "proto3"; package first; service API {}\n')
    second.write_text('syntax = "proto3"; package second; service API {}\n')
    protos = (second, first) if reverse else (first, second)
    schema = SimpleNamespace(
        proto=tuple(map(str, protos)),
        proto_path=(),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(SimpleNamespace(schema=schema, timeout=1.0))

    assert source.list_services() == ("first.API", "second.API")


@pytest.mark.skipif(os.name != "nt", reason="Windows drive semantics")
def test_cross_drive_proto_collisions_are_reported_as_cli_errors() -> None:
    with pytest.raises(CliError, match="different drives"):
        _common_entrypoint_root(
            (Path("C:/first/service.proto"), Path("D:/second/service.proto"))
        )


def test_unrepresentable_proto_root_is_reported_as_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_common_path(_paths: object) -> str:
        raise ValueError("paths are on different drives")

    monkeypatch.setattr(os.path, "commonpath", fail_common_path)

    with pytest.raises(CliError, match="different drives"):
        _common_entrypoint_root(
            (Path("first/service.proto"), Path("second/service.proto"))
        )


def test_nonexistent_proto_root_is_not_passed_to_protoc(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    requested = tmp_path / "service.proto"
    requested.write_text(
        'syntax = "proto3"; package intended; service API {}\n'
    )
    schema = SimpleNamespace(
        proto=(str(requested),),
        proto_path=(str(tmp_path / "missing-default"),),
        protoset=(),
        protoset_out=None,
        use_reflection=False,
    )

    source = ProtoDescriptorSource(SimpleNamespace(schema=schema, timeout=1.0))

    assert source.list_services() == ("intended.API",)
    assert "warning" not in capfd.readouterr().err.lower()


def test_protocol_unimplemented_response_falls_back_without_pinning_v1() -> (
    None
):
    paths: list[str] = []

    class Channel:
        def stream_stream(
            self, path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                paths.append(path)
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if ".v1." in path:
                    response.error_response.error_code = (
                        _StatusCode.UNIMPLEMENTED.value[0]
                    )
                    response.error_response.error_message = (
                        "stable reflection is disabled"
                    )
                else:
                    response.list_services_response.service.add(name="edge.API")
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    source = ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())

    assert source.list_services() == ("edge.API",)
    assert source.database.version == "v1alpha"
    assert paths == [
        "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
        "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
    ]


def test_reflection_response_double_need_not_implement_which_oneof() -> None:
    class Response:
        error_response = SimpleNamespace(error_code=0, error_message="")
        file_descriptor_response = SimpleNamespace(file_descriptor_proto=())
        all_extension_numbers_response = SimpleNamespace(extension_number=())
        list_services_response = SimpleNamespace(
            service=(SimpleNamespace(name="edge.API"),)
        )

        valid_host = ""

        def __init__(self, original_request: Any) -> None:
            self.original_request = original_request

        def HasField(self, field_name: str) -> bool:
            return field_name in {"original_request", "list_services_response"}

    class Channel:
        def stream_stream(self, _path: str, **_kwargs: Any) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                return _Call(Response(next(requests)))

            return rpc

    source = ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())

    assert source.list_services() == ("edge.API",)


def test_reflection_rejects_response_variant_for_another_request() -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.file_descriptor_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    with pytest.raises(CliError, match="expected 'list_services_response'"):
        ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())


def test_reflection_rejects_corrupt_file_descriptor_payload() -> None:
    cancelled: list[bool] = []

    class RecordingCall(_Call):
        def cancel(self) -> None:
            cancelled.append(True)
            raise RuntimeError("cancel cleanup")

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> RecordingCall:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.file_descriptor_response.file_descriptor_proto.append(
                    b"\xff"
                )
                return RecordingCall(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError) as raised:
        database.query(file_by_filename="corrupt.proto")

    assert str(raised.value) == (
        "reflection protocol error: invalid file descriptor payload"
    )
    assert raised.value.__cause__ is None
    assert cancelled == [True]
    assert getattr(raised.value, "_protodial_notes") == (
        "reflection cancellation also failed: cancel cleanup",
    )


@pytest.mark.parametrize(
    "payload",
    [
        descriptor_pb2.FileDescriptorProto().SerializeToString(),
        descriptor_pb2.FileDescriptorProto(
            name="invalid.proto", syntax="not-a-proto-syntax"
        ).SerializeToString(),
    ],
)
def test_reflection_rejects_semantically_invalid_descriptor_payload(
    payload: bytes,
) -> None:
    database = reflection_database(
        _DescriptorBatchChannel((payload,)), [], 1.0, _grpc()
    )

    with pytest.raises(CliError) as raised:
        database.query(file_by_filename="invalid.proto")

    assert str(raised.value) == (
        "reflection protocol error: invalid file descriptor payload"
    )
    assert raised.value.__cause__ is None
    assert database.file_protos == {}


@pytest.mark.parametrize(
    "bad_payload",
    [
        b"\xff",
        descriptor_pb2.FileDescriptorProto(
            name="invalid.proto", syntax="invalid"
        ).SerializeToString(),
    ],
)
def test_reflection_descriptor_batch_is_transactional(
    bad_payload: bytes,
) -> None:
    committed = descriptor_pb2.FileDescriptorProto(
        name="committed.proto", syntax="proto3"
    )
    candidate = descriptor_pb2.FileDescriptorProto(
        name="candidate.proto", syntax="proto3"
    )
    database = reflection_database(
        _DescriptorBatchChannel(
            (committed.SerializeToString(),),
            (candidate.SerializeToString(), bad_payload),
        ),
        [],
        1.0,
        _grpc(),
    )
    database.query(file_by_filename=committed.name)

    with pytest.raises(CliError, match="invalid file descriptor payload"):
        database.query(file_by_filename=candidate.name)

    assert tuple(database.file_protos) == (committed.name,)
    with pytest.raises(KeyError):
        descriptor_database.DescriptorDatabase.FindFileByName(
            database, candidate.name
        )
    source = IndexedDescriptorSource(
        descriptor_pool.DescriptorPool(database), (), database.file_protos
    )
    assert [file.name for file in descriptor_set(source).file] == [
        committed.name
    ]


def test_reflection_conflicting_batch_is_transactional() -> None:
    committed = descriptor_pb2.FileDescriptorProto(
        name="shared.proto", package="original", syntax="proto3"
    )
    candidate = descriptor_pb2.FileDescriptorProto(
        name="candidate.proto", syntax="proto3"
    )
    conflict = descriptor_pb2.FileDescriptorProto(
        name=committed.name, package="conflict", syntax="proto3"
    )
    database = reflection_database(
        _DescriptorBatchChannel(
            (committed.SerializeToString(),),
            (candidate.SerializeToString(), conflict.SerializeToString()),
        ),
        [],
        1.0,
        _grpc(),
    )
    database.query(file_by_filename=committed.name)

    with pytest.raises(CliError, match="invalid file descriptor payload"):
        database.query(file_by_filename=candidate.name)

    assert tuple(database.file_protos) == (committed.name,)
    assert database.file_protos[committed.name].package == "original"
    with pytest.raises(KeyError):
        descriptor_database.DescriptorDatabase.FindFileByName(
            database, candidate.name
        )


def test_reflection_descriptor_batch_allows_late_dependency() -> None:
    dependency = descriptor_pb2.FileDescriptorProto(
        name="dependency.proto", package="dependency", syntax="proto3"
    )
    dependency.message_type.add(name="Message")
    dependent = descriptor_pb2.FileDescriptorProto(
        name="dependent.proto", package="dependent", syntax="proto3"
    )
    dependent.dependency.append(dependency.name)
    dependent.message_type.add(name="UsesDependency").field.add(
        name="value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".dependency.Message",
    )
    database = reflection_database(
        _DescriptorBatchChannel(
            (dependent.SerializeToString(), dependency.SerializeToString())
        ),
        [],
        1.0,
        _grpc(),
    )

    database.query(file_by_filename=dependent.name)

    pool = descriptor_pool.DescriptorPool(database)
    assert pool.FindFileByName(dependent.name).name == dependent.name
    assert tuple(database.file_protos) == (dependent.name, dependency.name)


def test_reflection_identical_descriptor_duplicates_remain_idempotent() -> None:
    proto = descriptor_pb2.FileDescriptorProto(
        name="same.proto", syntax="proto3"
    )
    payload = proto.SerializeToString()
    database = reflection_database(
        _DescriptorBatchChannel((payload, payload), (payload,)),
        [],
        1.0,
        _grpc(),
    )

    database.query(file_by_filename=proto.name)
    database.query(file_by_filename=proto.name)

    assert tuple(database.file_protos) == (proto.name,)


@pytest.mark.parametrize(
    ("echo_field", "echo_value"),
    [
        ("file_by_filename", "other.proto"),
        ("file_containing_symbol", "other.Symbol"),
    ],
)
def test_reflection_rejects_same_variant_response_for_another_request(
    echo_field: str, echo_value: str
) -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(_requests: Any, **_call_kwargs: Any) -> _Call:
                response = reflection_pb2.ServerReflectionResponse()
                setattr(response.original_request, echo_field, echo_value)
                response.file_descriptor_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError, match="original_request does not match"):
        database.query(file_containing_symbol="wanted.Symbol")


def test_reflection_rejects_missing_request_echo() -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(_requests: Any, **_call_kwargs: Any) -> _Call:
                response = reflection_pb2.ServerReflectionResponse()
                response.list_services_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError, match="no original_request"):
        database.query(list_services="")


def test_reflection_rejects_wrong_valid_host() -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse(
                    valid_host="wrong.example"
                )
                _echo_request(response, request)
                response.list_services_response.SetInParent()
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    database = reflection_database(
        Channel(), [], 1.0, _grpc(), host="right.example"
    )

    with pytest.raises(CliError, match="valid_host 'wrong.example'"):
        database.query(list_services="")


def test_reflection_preserves_primary_when_cancellation_fails() -> None:
    primary = CliError("reflection primary")

    class FailingCall:
        def __iter__(self) -> FailingCall:
            return self

        def __next__(self) -> Any:
            raise primary

        def cancel(self) -> None:
            raise OSError("cancel cleanup")

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> FailingCall:
                next(requests)
                return FailingCall()

            return rpc

    database = reflection_database(Channel(), [], 1.0, _grpc())

    with pytest.raises(CliError, match="reflection primary") as raised:
        database.query(list_services="")

    assert raised.value is primary
    assert getattr(primary, "_protodial_notes") == (
        "reflection cancellation also failed: cancel cleanup",
    )


@pytest.mark.parametrize(
    "name",
    [
        "safe.API\nforged",
        "safe.API\rforged",
        "safe.API\x1b[2Jforged",
        "safe.API\u202eforged",
    ],
)
def test_reflection_rejects_invalid_service_names(name: str) -> None:
    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(requests: Any, **_call_kwargs: Any) -> _Call:
                request = next(requests)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                response.list_services_response.service.add(name=name)
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    with pytest.raises(
        CliError,
        match=r"^reflection protocol error: invalid service name$",
    ) as raised:
        ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())

    assert "safe.API" not in str(raised.value)


def test_reflection_discovers_extension_file_and_all_numbers() -> None:
    base = descriptor_pb2.FileDescriptorProto(
        name="base.proto", package="edge", syntax="proto2"
    )
    message = base.message_type.add(name="Base")
    message.extension_range.add(start=100, end=200)

    extensions = descriptor_pb2.FileDescriptorProto(
        name="extensions.proto", package="edge", syntax="proto2"
    )
    extensions.dependency.append("base.proto")
    extensions.extension.add(
        name="marker",
        number=123,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        extendee=".edge.Base",
    )
    requests: list[str] = []

    class Channel:
        def stream_stream(
            self, _path: str, *, response_deserializer: Any, **_kwargs: Any
        ) -> Any:
            def rpc(request_iterator: Any, **_call_kwargs: Any) -> _Call:
                request = next(request_iterator)
                kind = request.WhichOneof("message_request")
                requests.append(kind)
                response = reflection_pb2.ServerReflectionResponse()
                _echo_request(response, request)
                if kind == "list_services":
                    response.list_services_response.SetInParent()
                elif kind == "file_containing_symbol":
                    response.file_descriptor_response.file_descriptor_proto.append(
                        base.SerializeToString()
                    )
                elif kind == "file_containing_extension":
                    response.file_descriptor_response.file_descriptor_proto.extend(
                        (
                            extensions.SerializeToString(),
                            base.SerializeToString(),
                        )
                    )
                elif kind == "all_extension_numbers_of_type":
                    response.all_extension_numbers_response.base_type_name = (
                        "edge.Base"
                    )
                    response.all_extension_numbers_response.extension_number.append(
                        123
                    )
                else:
                    raise AssertionError(kind)
                return _Call(
                    response_deserializer(response.SerializeToString())
                )

            return rpc

    source = ReflectionDescriptorSource(Channel(), [], 1.0, _grpc())
    base_descriptor = source.pool.FindMessageTypeByName("edge.Base")

    extension = source.pool.FindExtensionByNumber(base_descriptor, 123)
    all_extensions = source.pool.FindAllExtensions(base_descriptor)

    assert extension.full_name == "edge.marker"
    assert [item.number for item in all_extensions] == [123]
    assert "file_containing_extension" in requests
    assert "all_extension_numbers_of_type" in requests


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
