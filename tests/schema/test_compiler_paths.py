from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from protodial.argument_parser import parse_arguments
from protodial.errors import CliError
from protodial.schema.local import (
    ProtoDescriptorSource,
    _common_entrypoint_root,
    _compile_proto_registry,
    _protoc_arguments,
    _ProtoCompilerInputs,
)


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
