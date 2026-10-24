"""Translate operational exceptions into structured diagnostic events."""

from __future__ import annotations

import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import singledispatch

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
    TlsMaterialSource,
)
from ...formats import MessageFormat
from ...schema.source_locations import utf8_byte_column_offset
from .models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
    DiagnosticValue,
)

_DECLARED_DEPENDENCIES = {
    "cryptography": "cryptography",
    "grpc": "grpcio",
    "grpc_reflection": "grpcio-reflection",
    "grpc_tools": "grpcio-tools",
    "google.protobuf": "protobuf",
    "google.rpc": "googleapis-common-protos",
}
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


def _notes(error: BaseException) -> tuple[str, ...]:
    """Return application notes on every supported Python version."""
    values = getattr(error, "_protodial_notes", None)
    if values is None:
        values = getattr(error, "__notes__", ())
    return tuple(str(value) for value in values)


def _missing_dependency(error: BaseException) -> DependencyError | None:
    """Translate a missing declared distribution to an actionable error."""
    if not isinstance(error, ImportError):
        return None
    missing = getattr(error, "name", None)
    if not isinstance(missing, str):
        return None
    distribution = next(
        (
            package
            for module, package in _DECLARED_DEPENDENCIES.items()
            if missing == module or missing.startswith(module + ".")
        ),
        None,
    )
    if distribution is None:
        return None
    return DependencyError(
        f"required dependency `{distribution}` is unavailable; reinstall with "
        f"`python3 -m pip install {distribution}`"
    )


def _input_span(command: Command, message: str) -> DiagnosticSpan | None:
    """Locate a parse failure in literal, file, or standard input."""
    import re

    options = command.invoke_options
    if options is None or options.data is None:
        return None
    match = re.search(r"\bat line (\d+), column (\d+)(?=\s*:|$)", message)
    if match is None:
        match = re.match(
            r"invalid text request \d+:\s*(\d+):(\d+)\s*:", message
        )
    line = int(match.group(1)) if match else None
    column = int(match.group(2)) if match else None
    data = options.data
    source = "--data"
    text: str | None = None
    if data in ("-", "@-"):
        source = "<stdin>"
    elif data.startswith("@"):
        source = data[1:]
        try:
            contents = pathlib.Path(source).read_text(encoding="utf-8")
            lines = contents.splitlines()
            if not lines or contents.endswith(("\n", "\r")):
                lines.append("")
            if line is not None and 0 < line <= len(lines):
                text = lines[line - 1]
        except (OSError, UnicodeError):
            pass
    else:
        lines = data.splitlines()
        if not lines or data.endswith(("\n", "\r")):
            lines.append("")
        if line is None:
            text = lines[0]
        elif 0 < line <= len(lines):
            text = lines[line - 1]
    start = (
        min(len(text), max(0, (column or 1) - 1)) if text is not None else None
    )
    end = (
        None
        if start is None
        else start
        if start == len(text or "")
        else start + 1
    )
    return DiagnosticSpan(
        source,
        line=line,
        column=column,
        text=text,
        start=start,
        end=end,
        label="invalid input",
    )


def _option_path_span(
    argv: Sequence[str],
    error: LocalFileError | DescriptorExportError | TlsMaterialSource,
    *,
    label: str = "path could not be read",
) -> DiagnosticSpan:
    """Locate the exact configured path, including repeated options."""
    matches: list[DiagnosticSpan] = []
    option_strings = getattr(error, "option_strings", (error.argument,))
    option_value = getattr(error, "option_value", error.path)

    def offsets(text: str) -> tuple[int, int] | None:
        if text != option_value:
            return None
        start = text.rfind(error.path)
        if start < 0:
            return None
        return start, start + len(error.path)

    for index, token in enumerate(argv):
        for option in option_strings:
            if token == option and index + 1 < len(argv):
                value = argv[index + 1]
                span_offsets = offsets(value)
                if span_offsets is not None:
                    matches.append(
                        DiagnosticSpan(
                            f"argument {index + 2}",
                            text=value,
                            start=span_offsets[0],
                            end=span_offsets[1],
                            label=label,
                        )
                    )
                break
            prefix = option + "=" if option.startswith("--") else option
            if token.startswith(prefix) and token != option:
                value = token[len(prefix) :]
                span_offsets = offsets(value)
                if span_offsets is not None:
                    matches.append(
                        DiagnosticSpan(
                            f"argument {index + 1}",
                            text=token,
                            start=len(prefix) + span_offsets[0],
                            end=len(prefix) + span_offsets[1],
                            label=label,
                        )
                    )
                break
    if matches:
        try:
            return matches[error.occurrence]
        except IndexError:
            pass
    return DiagnosticSpan(error.path, label=label)


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


def _operand_span(
    argv: Sequence[str], value: str, label: str
) -> DiagnosticSpan | None:
    """Locate an operation operand, including a method embedded in a URL."""
    if not value:
        return None
    for index in range(len(argv) - 1, -1, -1):
        token = argv[index]
        if token == value:
            return DiagnosticSpan(
                f"argument {index + 1}",
                text=token,
                start=0,
                end=len(value),
                label=label,
            )
    for index in range(len(argv) - 1, -1, -1):
        token = argv[index]
        start = token.find(value)
        if start < 0 or (start > 0 and token[start - 1] not in {"/", "="}):
            continue
        return DiagnosticSpan(
            f"argument {index + 1}",
            text=token,
            start=start,
            end=start + len(value),
            label=label,
        )
    return None


def _schema_input_span(error: SchemaInputError) -> DiagnosticSpan:
    """Build a source span for a schema decoder or compiler failure."""
    text: str | None = None
    if error.line is not None:
        try:
            contents = pathlib.Path(error.path).read_text(encoding="utf-8")
            lines = contents.splitlines()
            if not lines or contents.endswith(("\n", "\r")):
                lines.append("")
            if 0 < error.line <= len(lines):
                text = lines[error.line - 1]
        except (OSError, UnicodeError):
            pass
    start = None
    if text is not None and error.column is not None:
        start = (
            utf8_byte_column_offset(text, error.column)
            if isinstance(error, ProtoCompilationError)
            else min(len(text), max(0, error.column - 1))
        )
    end = (
        None
        if start is None
        else start
        if start == len(text or "")
        else start + 1
    )
    return DiagnosticSpan(
        error.path,
        line=error.line,
        column=error.column,
        text=text,
        start=start,
        end=end,
        label="invalid schema input",
    )


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
            _option_path_span(
                context.argv, source, label="invalid TLS material"
            )
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
        spans=(_option_path_span(context.argv, error),),
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
            _option_path_span(
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
        spans=(_schema_input_span(error),),
        causes=(error.reason,) if error.reason else (),
        help=(error.help,) if error.help else (),
    )


def _describe_message_parse(
    error: BaseException, context: _ErrorContext
) -> _ErrorPresentation:
    """Describe a request body that could not be parsed."""
    command = context.command
    span = (
        _input_span(command, context.message) if command is not None else None
    )
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
        _operand_span(context.argv, operation_symbol(command), label)
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
    from ...message_io.models import MessageError, MessageParseError

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
        else _missing_dependency(error)
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
        notes=_notes(presented_error),
    )
