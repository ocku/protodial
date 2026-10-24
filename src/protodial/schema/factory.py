"""Construct descriptor sources from CLI configuration."""

from __future__ import annotations

from ..commands.models import Command
from ..request_metadata.types import Metadata
from .composition import CompositeDescriptorSource
from .contracts import DescriptorSource, GrpcModule, ReflectionChannel
from .local import command_proto_paths, local_descriptor_source
from .reflection import ReflectionDescriptorSource


def build_descriptor_source(
    command: Command,
    grpc: GrpcModule,
    channel: ReflectionChannel | None = None,
    metadata: Metadata | None = None,
    reflection_host: str = "",
) -> DescriptorSource:
    """Build the schema source shared by invocation and discovery paths."""

    has_local = bool(command_proto_paths(command) or command.schema.protoset)
    use_reflection = (
        bool(getattr(command.schema, "use_reflection", False)) or not has_local
    )
    local = local_descriptor_source(command) if has_local else None
    if not use_reflection:
        if local is None:
            raise TypeError(
                "a descriptor source requires local schema or reflection"
            )
        return local
    if channel is None:
        raise TypeError("reflection descriptor source requires a channel")
    reflection = ReflectionDescriptorSource(
        channel,
        metadata if metadata is not None else [],
        command.timeout,
        grpc,
        getattr(local, "registry", None),
        reflection_host,
    )
    return (
        CompositeDescriptorSource(local, reflection)
        if local is not None
        else reflection
    )
