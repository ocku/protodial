"""Export resolved schemas as descriptor sets."""

from __future__ import annotations

import os
import pathlib
import tempfile
from typing import cast

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FileDescriptor

from ..errors import add_error_note
from .contracts import DescriptorSource


def descriptor_set(
    source: DescriptorSource,
) -> descriptor_pb2.FileDescriptorSet:
    """Materialize known service files in stable dependency order."""

    roots: dict[str, FileDescriptor] = {}
    for service_name in source.list_services():
        service = source.pool.FindServiceByName(service_name)
        roots[service.file.name] = service.file

    file_protos = cast(
        dict[str, descriptor_pb2.FileDescriptorProto],
        getattr(source, "file_protos", {}),
    )
    for file_name in file_protos:
        roots[file_name] = source.pool.FindFileByName(file_name)

    ordered: list[descriptor_pb2.FileDescriptorProto] = []
    visited: set[str] = set()

    def visit(file_descriptor: FileDescriptor) -> None:
        if file_descriptor.name in visited:
            return
        visited.add(file_descriptor.name)
        for dependency in sorted(
            file_descriptor.dependencies, key=lambda item: item.name
        ):
            visit(dependency)
        proto = file_protos.get(file_descriptor.name)
        if proto is None:
            proto = descriptor_pb2.FileDescriptorProto()
            file_descriptor.CopyToProto(proto)
        ordered.append(proto)

    for file_descriptor in sorted(roots.values(), key=lambda item: item.name):
        visit(file_descriptor)
    return descriptor_pb2.FileDescriptorSet(file=ordered)


def write_descriptor_set(
    source: DescriptorSource, path: str | pathlib.Path
) -> None:
    """Atomically export a complete binary FileDescriptorSet."""

    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = descriptor_set(source).SerializeToString(deterministic=True)
    temporary: pathlib.Path | None = None
    primary: BaseException | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as output:
            temporary = pathlib.Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    except BaseException as error:
        primary = error
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                reason = (
                    cleanup_error.strerror
                    if isinstance(cleanup_error, OSError)
                    and cleanup_error.strerror
                    else str(cleanup_error)
                )
                add_error_note(
                    primary,
                    f"descriptor temporary-file cleanup also failed: {reason}",
                )


export_descriptor_set = write_descriptor_set
