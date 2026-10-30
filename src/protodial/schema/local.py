"""Load descriptors from local proto and protoset files."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass

from google.protobuf import descriptor_pb2
from google.protobuf.message import DecodeError

from ..commands.models import Command
from ..errors import (
    CliError,
    DependencyError,
    LocalFileError,
    SchemaInputError,
)
from .compiler_diagnostics import compiler_error as _compiler_error
from .compiler_diagnostics import (
    emit_compiler_warnings as _emit_compiler_warnings,
)
from .registry import (
    DescriptorBuildError,
    FileDescriptorRegistry,
    IndexedDescriptorSource,
    source_from_registry,
)


def _bundled_proto_root() -> pathlib.Path | None:
    """Locate grpc_tools' bundled well-known protos without importing protoc."""
    spec = importlib.util.find_spec("grpc_tools")
    if spec is None:
        return None
    locations = spec.submodule_search_locations
    if locations is None:
        return None
    for location in locations:
        root = pathlib.Path(location) / "_proto"
        if root.is_dir():
            return root.resolve()
    return None


def _require_protoc() -> None:
    """Fail before spawning Python when grpcio-tools is unavailable."""
    try:
        available = importlib.util.find_spec("grpc_tools.protoc") is not None
    except (ImportError, AttributeError):
        available = False
    if not available:
        raise DependencyError(
            "grpcio-tools is required to compile local .proto files; "
            "install it with `python -m pip install grpcio-tools`"
        )


def command_proto_paths(command: Command) -> tuple[str, ...]:
    """Return explicit proto files configured by a command."""
    value = command.schema.proto
    if not value:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


def _common_entrypoint_root(
    proto_files: Sequence[pathlib.Path],
) -> pathlib.Path:
    """Return one root for colliding entrypoints on the same drive."""
    try:
        return pathlib.Path(
            os.path.commonpath([path.parent for path in proto_files])
        )
    except ValueError as error:
        raise CliError(
            "proto entrypoints on different drives have colliding import "
            "paths; rename an entrypoint or compile the schemas separately"
        ) from error


@dataclass(frozen=True)
class _ProtoCompilerInputs:
    """Resolved filesystem and import names passed to protoc."""

    files: tuple[pathlib.Path, ...]
    roots: tuple[pathlib.Path, ...]
    relative_files: tuple[pathlib.Path, ...]


def _readable_proto_files(command: Command) -> tuple[pathlib.Path, ...]:
    """Validate configured source paths before resolving filesystem aliases."""
    configured = command_proto_paths(command)
    path_occurrences: dict[str, int] = {}
    for path in configured:
        occurrence = path_occurrences.get(path, 0)
        path_occurrences[path] = occurrence + 1
        try:
            with pathlib.Path(path).open("rb"):
                pass
        except OSError as error:
            raise LocalFileError(
                "proto source file",
                path,
                "--proto",
                error,
                occurrence=occurrence,
            ) from error
    return tuple(
        dict.fromkeys(pathlib.Path(path).absolute() for path in configured)
    )


def _proto_import_roots(
    command: Command, proto_files: Sequence[pathlib.Path]
) -> list[pathlib.Path]:
    """Build ordered explicit, implicit, and bundled import roots."""
    explicit_roots = tuple(
        path.resolve()
        for value in command.schema.proto_path
        if (path := pathlib.Path(value).absolute()).is_dir()
    )
    implicit_roots = sorted(
        {path.parent for path in proto_files},
        key=lambda path: (len(path.parts), str(path)),
    )
    roots = list(dict.fromkeys((*explicit_roots, *implicit_roots)))
    bundled_root = _bundled_proto_root()
    if bundled_root is not None:
        roots.append(bundled_root)
    return roots


def _relative_proto_files(
    proto_files: Sequence[pathlib.Path],
    roots: Sequence[pathlib.Path],
) -> list[pathlib.Path]:
    """Resolve each entrypoint to its first containing import root."""
    relative_files: list[pathlib.Path] = []
    for proto_file in proto_files:
        root = next(
            (
                candidate
                for candidate in roots
                if proto_file.is_relative_to(candidate)
            ),
            None,
        )
        if root is None:
            raise CliError(
                f"proto file is outside all import roots: {proto_file}"
            )
        relative_files.append(proto_file.relative_to(root))
    return relative_files


def _first_resolved_import(
    relative_file: pathlib.Path, roots: Sequence[pathlib.Path]
) -> pathlib.Path | None:
    """Return the first existing file protoc resolves for an import name."""
    return next(
        (
            (root / relative_file).resolve()
            for root in roots
            if (root / relative_file).is_file()
        ),
        None,
    )


def _has_shadowed_entrypoint(
    proto_files: Sequence[pathlib.Path],
    relative_files: Sequence[pathlib.Path],
    roots: Sequence[pathlib.Path],
) -> bool:
    """Return whether root ordering selects a different entrypoint file."""
    return any(
        _first_resolved_import(relative_file, roots) != proto_file
        for proto_file, relative_file in zip(
            proto_files, relative_files, strict=True
        )
    )


def _disambiguate_entrypoints(
    proto_files: tuple[pathlib.Path, ...],
    roots: list[pathlib.Path],
    relative_files: list[pathlib.Path],
) -> tuple[tuple[pathlib.Path, ...], tuple[pathlib.Path, ...]]:
    """Add a common root when import names collide or shadow entrypoints."""
    duplicates = len(set(relative_files)) != len(relative_files)
    if not duplicates and not _has_shadowed_entrypoint(
        proto_files, relative_files, roots
    ):
        return tuple(roots), tuple(relative_files)

    common_root = _common_entrypoint_root(proto_files)
    roots.insert(0, common_root)
    relative_files = [path.relative_to(common_root) for path in proto_files]
    if len(set(relative_files)) != len(relative_files):
        raise CliError("proto entrypoints resolve to duplicate import paths")
    return tuple(roots), tuple(relative_files)


def _resolve_compiler_inputs(command: Command) -> _ProtoCompilerInputs:
    """Validate and resolve local proto compilation inputs."""
    requested_files = _readable_proto_files(command)
    _require_protoc()
    # Filesystem identities, rather than symlink spellings, determine imports.
    proto_files = tuple(
        dict.fromkeys(path.resolve() for path in requested_files)
    )
    roots = _proto_import_roots(command, proto_files)
    relative_files = _relative_proto_files(proto_files, roots)
    roots_tuple, relative_tuple = _disambiguate_entrypoints(
        proto_files, roots, relative_files
    )
    return _ProtoCompilerInputs(proto_files, roots_tuple, relative_tuple)


def _protoc_arguments(
    descriptor_path: pathlib.Path, inputs: _ProtoCompilerInputs
) -> list[str]:
    """Build the deterministic protoc subprocess argument list."""
    return [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        *(f"-I{root}" for root in inputs.roots),
        f"--descriptor_set_out={descriptor_path}",
        "--include_imports",
        "--include_source_info",
        # Entrypoints are proto import names, not native filesystem paths.
        *(path.as_posix() for path in inputs.relative_files),
    ]


def _compile_descriptor_set(
    inputs: _ProtoCompilerInputs,
    timeout: float | None = None,
) -> descriptor_pb2.FileDescriptorSet:
    """Run protoc and decode its descriptor-set output."""
    with tempfile.TemporaryDirectory(
        prefix="protodial-"
    ) as temporary_directory:
        descriptor_path = pathlib.Path(temporary_directory) / "schema.pb"
        completed = subprocess.run(
            _protoc_arguments(descriptor_path, inputs),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # Select posix_spawn where supported and avoid process-global
            # at-fork handlers in threaded gRPC embedders.
            close_fds=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
        compiler_output = (completed.stderr, completed.stdout)
        if completed.returncode:
            raise _compiler_error(compiler_output, inputs.roots)
        _emit_compiler_warnings(compiler_output, inputs.roots)
        try:
            return descriptor_pb2.FileDescriptorSet.FromString(
                descriptor_path.read_bytes()
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "protoc succeeded without a descriptor set"
            ) from error


def _registry_from_compilation(
    descriptor_set: descriptor_pb2.FileDescriptorSet,
    proto_files: Sequence[pathlib.Path],
) -> FileDescriptorRegistry:
    """Register compiled files with a source-specific conflict origin."""
    registry = FileDescriptorRegistry()
    origin = "proto compilation of " + ", ".join(
        str(path) for path in proto_files
    )
    for file_proto in descriptor_set.file:
        registry.add(file_proto, origin)
    return registry


def _compile_proto_registry(command: Command) -> FileDescriptorRegistry:
    """Compile configured proto entrypoints into an indexed registry."""
    inputs = _resolve_compiler_inputs(command)
    timeout = getattr(command, "_completion_compile_timeout", None)
    descriptor_set = _compile_descriptor_set(
        inputs,
        timeout=timeout if isinstance(timeout, (int, float)) else None,
    )
    return _registry_from_compilation(descriptor_set, inputs.files)


class ProtoDescriptorSource(IndexedDescriptorSource):
    """A descriptor source compiled from local proto files."""

    def __init__(self, command: Command) -> None:
        registry = _compile_proto_registry(command)
        source = source_from_registry(registry)
        super().__init__(
            source.pool, source.list_services(), source.file_protos
        )
        self.registry = registry


class ProtosetDescriptorSource(IndexedDescriptorSource):
    """A descriptor source loaded from descriptor-set files."""

    def __init__(self, paths: Sequence[str]) -> None:
        registry = FileDescriptorRegistry()
        descriptor_paths: dict[str, str] = {}
        path_occurrences: dict[str, int] = {}
        for path in paths:
            occurrence = path_occurrences.get(path, 0)
            path_occurrences[path] = occurrence + 1
            try:
                descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
                    pathlib.Path(path).read_bytes()
                )
            except OSError as error:
                raise LocalFileError(
                    "protoset file",
                    path,
                    "--protoset",
                    error,
                    occurrence=occurrence,
                ) from error
            except DecodeError as error:
                raise SchemaInputError(
                    "protoset is not a valid FileDescriptorSet",
                    path=path,
                    reason=(
                        str(error) or "protobuf wire data could not be decoded"
                    ),
                    help=(
                        "regenerate the descriptor set with protoc "
                        "--descriptor_set_out"
                    ),
                ) from error
            for file_proto in descriptor_set.file:
                if registry.add(file_proto, f"protoset {path}"):
                    descriptor_paths[file_proto.name] = path
        try:
            source = source_from_registry(registry)
        except DescriptorBuildError as error:
            path = descriptor_paths.get(error.file_name, paths[0])
            raise SchemaInputError(
                "protoset contains an invalid file descriptor",
                path=path,
                reason=f"{error.file_name}: {error.original}",
                help=(
                    "regenerate the descriptor set with protoc "
                    "--descriptor_set_out and --include_imports"
                ),
            ) from error
        super().__init__(
            source.pool, source.list_services(), source.file_protos
        )
        self.registry = registry


def proto_descriptor_source(command: Command) -> IndexedDescriptorSource:
    """Build a descriptor source from configured proto files."""
    return ProtoDescriptorSource(command)


def protoset_descriptor_source(paths: Sequence[str]) -> IndexedDescriptorSource:
    """Build a descriptor source from descriptor-set paths."""
    return ProtosetDescriptorSource(paths)


def local_descriptor_source(command: Command) -> IndexedDescriptorSource:
    """Combine all local schema inputs configured by a command."""
    proto_paths = command_proto_paths(command)
    protosets = tuple(command.schema.protoset)
    if not proto_paths and not protosets:
        raise TypeError(
            "local descriptor source requires --proto or --protoset"
        )
    if proto_paths and not protosets:
        return ProtoDescriptorSource(command)
    if protosets and not proto_paths:
        return ProtosetDescriptorSource(protosets)

    proto_source = ProtoDescriptorSource(command)
    registry = proto_source.registry
    if registry is None:
        raise RuntimeError("proto descriptor source has no registry")
    other = ProtosetDescriptorSource(protosets)
    if other.registry is None:
        raise RuntimeError("protoset descriptor source has no registry")
    for name, file_proto in other.file_protos.items():
        registry.add(file_proto, other.registry.origins[name])
    return source_from_registry(registry)
