"""Coordinate descriptor sources, reflection channels, and schema export."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..commands.models import Command
from ..errors import (
    CliError,
    DescriptorExportError,
    add_error_note,
    add_secondary_error,
)
from ..transport_core.protocols import Channel
from .contracts import DescriptorSource, GrpcModule, ReflectionChannel
from .export import write_descriptor_set
from .factory import build_descriptor_source
from .local import command_proto_paths

if TYPE_CHECKING:
    from ..presentation.diagnostics.models import DiagnosticSink
    from ..request_metadata.parsing import MetadataScopes


@dataclass(frozen=True)
class DescriptorSession:
    """Resources shared by schema discovery and RPC invocation."""

    source: DescriptorSource
    channel: Channel | None
    metadata: MetadataScopes
    sink: DiagnosticSink


@dataclass(frozen=True)
class _DescriptorPlan:
    metadata: MetadataScopes
    sink: DiagnosticSink
    has_local: bool
    use_reflection: bool


def _reflection_host(command: Command) -> str:
    """Return an HTTP/2 authority suitable for a reflection request."""

    return command.transport.authority or command.target.authority or ""


@contextmanager
def _export_after_use(
    source: DescriptorSource, path: str | None
) -> Generator[None, None, None]:
    """Export a source without replacing an exception from its primary work."""

    try:
        yield
    except BaseException as primary:
        if path:
            try:
                write_descriptor_set(source, path)
            except Exception as export_error:
                wrapped = DescriptorExportError(path, export_error)
                for note in getattr(export_error, "_protodial_notes", ()):
                    add_error_note(wrapped, str(note))
                add_secondary_error(
                    primary, "descriptor export also failed", wrapped
                )
        raise
    else:
        if path:
            try:
                write_descriptor_set(source, path)
            except Exception as export_error:
                wrapped = DescriptorExportError(path, export_error)
                for note in getattr(export_error, "_protodial_notes", ()):
                    add_error_note(wrapped, str(note))
                raise wrapped from export_error


def _descriptor_plan(command: Command) -> _DescriptorPlan:
    from ..commands.operations import Invoke
    from ..presentation.diagnostics import StderrDiagnosticSink
    from ..presentation.diagnostics.context import current_sink
    from ..request_metadata.parsing import MetadataError, parse_metadata_scopes

    try:
        metadata = parse_metadata_scopes(
            command.metadata.header,
            command.metadata.reflect_header,
            command.metadata.rpc_header,
            expand=command.metadata.expand_headers,
        )
    except MetadataError as error:
        raise CliError(str(error)) from error
    if command.metadata.rpc_header and not isinstance(
        command.operation, Invoke
    ):
        raise CliError("--rpc-header can only be used when invoking a method")

    has_local = bool(command_proto_paths(command) or command.schema.protoset)
    use_reflection = command.schema.use_reflection or not has_local
    if command.metadata.reflect_header and not use_reflection:
        raise CliError("--reflect-header requires reflection")

    sink = current_sink() or StderrDiagnosticSink(
        command.diagnostics.verbosity,
        diagnostic_format=command.diagnostics.diagnostic_format,
        color=command.diagnostics.color,
    )
    return _DescriptorPlan(metadata, sink, has_local, use_reflection)


def _build_source(
    command: Command,
    grpc: GrpcModule,
    plan: _DescriptorPlan,
    channel: Channel | None,
) -> DescriptorSource:
    return build_descriptor_source(
        command,
        grpc,
        cast(ReflectionChannel, channel) if plan.use_reflection else None,
        plan.metadata.reflection,
        _reflection_host(command),
    )


def _emit_schema_source(plan: _DescriptorPlan) -> None:
    from ..presentation.diagnostics import DiagnosticEvent

    if plan.has_local and plan.use_reflection:
        label = "local+reflection"
    elif plan.has_local:
        label = "local"
    else:
        label = "reflection"
    plan.sink.emit(
        DiagnosticEvent(
            "schema.source",
            level=2,
            fields={"source": label},
            metadata=tuple(plan.metadata.reflection),
        )
    )


@contextmanager
def _opened_session(
    source: DescriptorSource,
    channel: Channel | None,
    plan: _DescriptorPlan,
    export_path: str | None,
) -> Generator[DescriptorSession, None, None]:
    from ..presentation.diagnostics.context import use_sink

    with _export_after_use(source, export_path):
        with use_sink(plan.sink):
            yield DescriptorSession(source, channel, plan.metadata, plan.sink)


@contextmanager
def open_descriptor_session(
    command: Command,
    grpc: GrpcModule,
    *,
    require_channel: bool = False,
) -> Generator[DescriptorSession, None, None]:
    """Open metadata, diagnostics, schema, and an optional shared channel."""

    from ..presentation.diagnostics import DiagnosticEvent
    from ..presentation.diagnostics.context import use_sink
    from ..transport import ready_channel

    plan = _descriptor_plan(command)
    if not require_channel and not plan.use_reflection:
        source = _build_source(command, grpc, plan, None)
        _emit_schema_source(plan)
        with _opened_session(
            source, None, plan, command.schema.protoset_out
        ) as session:
            yield session
        return

    plan.sink.emit(
        DiagnosticEvent(
            "transport.connecting",
            level=2,
            fields={
                "target": command.target.channel,
                "transport": command.transport.mode,
            },
        )
    )
    invoke_options = command.invoke_options
    await_ready = not (
        require_channel
        and not plan.use_reflection
        and invoke_options is not None
        and invoke_options.wait_for_ready
    )
    with ready_channel(command, grpc, await_ready=await_ready) as channel:
        if await_ready:
            plan.sink.emit(
                DiagnosticEvent(
                    "transport.connected",
                    level=2,
                    fields={"target": command.target.channel},
                )
            )
        with use_sink(plan.sink):
            source = _build_source(command, grpc, plan, channel)
        _emit_schema_source(plan)
        with _opened_session(
            source, channel, plan, command.schema.protoset_out
        ) as session:
            yield session


@contextmanager
def open_descriptor_source(
    command: Command, grpc: GrpcModule
) -> Generator[DescriptorSource, None, None]:
    """Open a descriptor source for list and describe operations."""

    with open_descriptor_session(command, grpc) as session:
        yield session.source
