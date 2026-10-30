"""Translate operational exceptions into structured diagnostic events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import singledispatch
from typing import TYPE_CHECKING

from ...commands.models import Command
from ...commands.operations import Describe, Invoke
from ...errors import (
    CliError,
    DependencyError,
    DescriptorExportError,
    LocalFileError,
    LocalOutputError,
    ProtoCompilationError,
    SchemaInputError,
    TlsMaterialError,
    error_notes,
)
from ...formats import MessageFormat
from ._dependencies import (
    missing_dependency,
)
from ._error_spans import (
    input_span,
    operand_span,
    option_path_span,
    schema_input_span,
)
from .models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
    DiagnosticValue,
)

if TYPE_CHECKING:
    from ...message_io.models import JsonMessageSchemaError


_TRANSPORT_TERMS = ("tls", "certificate", "connection", "authority")
_TRANSPORT_HELP = ("check the target, network, TLS, and timeout settings",)


def _empty_fields() -> dict[str, DiagnosticValue]:
    """Create an independently mutable empty diagnostic field mapping."""
    return {}


@dataclass(frozen=True)
class _ErrorContext:
    """Inputs shared by exception-specific presentation handlers."""

    message: str
    command: Command | None
    argv: Sequence[str]


@dataclass(frozen=True)
class _ErrorPresentation:
    """Exception-specific content used to build a diagnostic event."""

    name: str
    message: str
    code: str
    fields: Mapping[str, DiagnosticValue] = field(default_factory=_empty_fields)
    spans: tuple[DiagnosticSpan, ...] = ()
    causes: tuple[str, ...] = ()
    help: tuple[str, ...] = ()


def _os_error_reason(error: BaseException) -> str:
    """Return stable OS detail without duplicating a path in the headline."""
    detail = error.strerror if isinstance(error, OSError) else None
    return detail or str(error) or "the operating system rejected it"


def operation_symbol(command: Command) -> str:
    """Return the schema operand selected by a command."""
    operation = command.operation
    if isinstance(operation, Invoke):
        return operation.method
    if isinstance(operation, Describe):
        return operation.symbol
    return operation.service or ""


def _transport_presentation(context: _ErrorContext) -> _ErrorPresentation:
    """Present a network or TLS failure with target context when available."""
    if context.command is None:
        return _ErrorPresentation(
            "transport.failed",
            context.message,
            "E3002",
            help=_TRANSPORT_HELP,
        )
    target = context.command.target.channel
    return _ErrorPresentation(
        "transport.failed",
        f"could not connect to `{target}`",
        "E3002",
        causes=(context.message,),
        help=_TRANSPORT_HELP,
    )


@singledispatch
def _describe_error(
    error: BaseException, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe an exception without a known user-facing category."""
    return _ErrorPresentation(
        "internal.failed",
        context.message,
        "E9001",
        help=("report this failure with the command and version",),
    )


@_describe_error.register
def _describe_dependency(
    error: DependencyError, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe an unavailable declared runtime dependency."""
    return _ErrorPresentation(
        "dependency.unavailable",
        context.message,
        "E8001",
        help=("reinstall protodial and its runtime dependencies",),
    )


@_describe_error.register
def _describe_tls_material(
    error: TlsMaterialError, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe invalid or inconsistent TLS files."""
    primary = error.sources[0]
    return _ErrorPresentation(
        "command.failed",
        context.message,
        "E2001",
        fields={
            "kind": error.kind,
            "path": primary.path,
            "argument": primary.argument,
            "sources": tuple(
                {"path": source.path, "argument": source.argument}
                for source in error.sources
            ),
        },
        spans=tuple(
            option_path_span(context.argv, source, label="invalid TLS material")
            for source in error.sources
        ),
        causes=(error.reason,),
        help=(error.help,),
    )


@_describe_error.register
def _describe_local_file(
    error: LocalFileError, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe a configured local input that could not be read."""
    fields: dict[str, DiagnosticValue] = {
        "operation": "read",
        "path": error.path,
        "argument": error.argument,
    }
    if error.original.errno is not None:
        fields["errno"] = error.original.errno
    return _ErrorPresentation(
        "local_io.failed",
        f"could not read {error.description} `{error.path}`",
        "E8002",
        fields=fields,
        spans=(option_path_span(context.argv, error),),
        causes=(_os_error_reason(error.original),),
        help=(
            f"check that `{error.path}` exists and is readable, then pass it "
            f"with `{error.argument}`",
        ),
    )


@_describe_error.register
def _describe_descriptor_export(
    error: DescriptorExportError, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe a descriptor-set destination that could not be written."""
    fields: dict[str, DiagnosticValue] = {
        "operation": "write",
        "path": error.path,
        "argument": error.argument,
    }
    if isinstance(error.original, OSError) and error.original.errno is not None:
        fields["errno"] = error.original.errno
    return _ErrorPresentation(
        "descriptor.export_failed",
        f"could not export descriptor set to `{error.path}`",
        "E8002",
        fields=fields,
        spans=(
            option_path_span(
                context.argv,
                error,
                label="export destination could not be written",
            ),
        ),
        causes=(_os_error_reason(error.original),),
        help=(
            f"check that the parent of `{error.path}` exists and is writable, "
            "and that sufficient disk space is available",
        ),
    )


@_describe_error.register
def _describe_local_output(
    error: LocalOutputError, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe a failed flush or write to a local output stream."""
    fields: dict[str, DiagnosticValue] = {
        "operation": error.operation,
        "stream": error.stream,
    }
    if error.original.errno is not None:
        fields["errno"] = error.original.errno
    return _ErrorPresentation(
        "local_io.failed",
        f"could not {error.operation} {error.stream}",
        "E8002",
        fields=fields,
        causes=(_os_error_reason(error.original),),
        help=(
            "check the output destination, its permissions, and available "
            "disk space",
        ),
    )


@_describe_error.register
def _describe_schema_input(
    error: SchemaInputError, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe invalid local schema source or descriptor data."""
    name, code = (
        ("command.failed", "E2001")
        if isinstance(error, ProtoCompilationError)
        else ("schema.input_invalid", "E2002")
    )
    return _ErrorPresentation(
        name,
        context.message,
        code,
        spans=(schema_input_span(error),),
        causes=(error.reason,) if error.reason else (),
        help=(error.help,) if error.help else (),
    )


def _describe_message_parse(
    error: BaseException, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe a request body that could not be parsed."""
    command = context.command
    span = input_span(command, context.message) if command is not None else None
    input_format = (
        command.invoke_options.input_format.value
        if command is not None and command.invoke_options is not None
        else "input"
    )
    help_items = (
        ("validate JSON input with `jq .`",)
        if command is not None
        and command.invoke_options is not None
        and command.invoke_options.input_format is MessageFormat.JSON
        else ()
    )
    return _ErrorPresentation(
        "input.invalid",
        f"request body is not valid {input_format}",
        "E2101",
        spans=(span,) if span is not None else (),
        causes=(context.message,),
        help=help_items,
    )


def _describe_json_message_schema(
    error: JsonMessageSchemaError, context: _ErrorContext
) -> _ErrorPresentation:
    """Distinguish valid JSON from an invalid ProtoJSON message shape."""
    command = context.command
    span = input_span(command, context.message) if command is not None else None
    return _ErrorPresentation(
        "input.invalid",
        "request body is valid JSON but does not match the ProtoJSON schema "
        f"for {error.message_type}",
        "E2101",
        fields={
            "kind": "protojson_schema",
            "request_type": error.message_type,
            "request_index": error.message_index,
        },
        spans=(span,) if span is not None else (),
        causes=(f"request {error.message_index}: {error.reason}",),
        help=(
            f"match field names and values to the {error.message_type} schema",
        ),
    )


def _describe_message_error(
    error: BaseException, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe invalid message I/O configuration."""
    return _ErrorPresentation("command.failed", context.message, "E2001")


@_describe_error.register
def _describe_value_error(
    error: ValueError, context: _ErrorContext
) -> _ErrorPresentation:
    """Recognize transport failures still exposed as generic value errors."""
    if any(term in context.message.lower() for term in _TRANSPORT_TERMS):
        return _transport_presentation(context)
    return _describe_error.dispatch(BaseException)(error, context)


@_describe_error.register(TimeoutError)
@_describe_error.register(ConnectionError)
def _describe_connection_error(
    error: BaseException, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe direct connection and timeout failures."""
    return _transport_presentation(context)


@_describe_error.register
def _describe_os_error(
    error: OSError, context: _ErrorContext
) -> _ErrorPresentation:
    """Separate transport-like OS failures from local I/O failures."""
    if any(
        term in context.message.lower()
        for term in ("tls", "certificate", "connection")
    ):
        return _transport_presentation(context)
    return _ErrorPresentation(
        "local_io.failed",
        context.message,
        "E8002",
        help=("check the path and local file permissions",),
    )


def _schema_symbol_presentation(
    context: _ErrorContext,
) -> _ErrorPresentation:
    """Describe a missing, ambiguous, or wrong-kind schema symbol."""
    message = context.message
    if "ambiguous symbol" in message:
        label = "ambiguous schema symbol"
        help_items = (
            "use a fully qualified symbol from the listed candidates",
        )
    elif "does not name a service" in message:
        label = "expected a service"
        help_items = (
            "run `protodial list TARGET` and pass one of the listed services",
        )
    elif "does not name a method" in message:
        label = "expected a method"
        help_items = (
            "run `protodial list TARGET SERVICE` and pass a listed "
            "SERVICE/METHOD",
        )
    else:
        label = "unknown schema symbol"
        help_items = (
            "run `protodial list TARGET` to inspect available services",
        )
    command = context.command
    span = (
        operand_span(context.argv, operation_symbol(command), label)
        if command is not None
        else None
    )
    return _ErrorPresentation(
        "schema.symbol_not_found",
        message,
        "E2004",
        spans=(span,) if span is not None else (),
        help=help_items,
    )


@_describe_error.register
def _describe_cli_error(
    error: CliError, context: _ErrorContext
) -> _ErrorPresentation:
    """Classify remaining domain errors by their stable public wording."""
    message = context.message
    if any(
        phrase in message.lower()
        for phrase in ("file not found", "cannot read", "no such file")
    ):
        return _ErrorPresentation(
            "local_io.failed",
            message,
            "E8002",
            help=("check the path and local file permissions",),
        )
    if any(
        phrase in message
        for phrase in (
            "does not name a method",
            "does not name a service",
            "symbol",
        )
    ):
        return _schema_symbol_presentation(context)
    return _ErrorPresentation("command.failed", message, "E2001")


def _describe_with_message_errors(
    error: BaseException, context: _ErrorContext
) -> _ErrorPresentation:
    """Dispatch after lazily loading message exception types."""
    from ...message_io.models import (
        JsonMessageSchemaError,
        MessageError,
        MessageParseError,
    )

    if isinstance(error, JsonMessageSchemaError):
        return _describe_json_message_schema(error, context)
    if isinstance(error, MessageParseError):
        return _describe_message_parse(error, context)
    if isinstance(error, MessageError):
        return _describe_message_error(error, context)
    return _describe_error(error, context)


def ordinary_error_event(
    error: BaseException,
    command: Command | None = None,
    argv: Sequence[str] = (),
) -> DiagnosticEvent:
    """Translate an operational exception into one structured error event."""
    dependency_error = (
        error
        if isinstance(error, DependencyError)
        else missing_dependency(error)
    )
    presented_error = dependency_error or error
    message = str(presented_error) or "the operation failed"
    context = _ErrorContext(message, command, argv)
    presentation = (
        _describe_error(presented_error, context)
        if dependency_error is not None
        else _describe_with_message_errors(presented_error, context)
    )
    return DiagnosticEvent(
        presentation.name,
        presentation.message,
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code=presentation.code,
        fields=presentation.fields,
        spans=presentation.spans,
        causes=presentation.causes,
        help=presentation.help,
        notes=error_notes(presented_error),
    )
