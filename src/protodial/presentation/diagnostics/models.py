"""Define presentation-neutral diagnostic events and sinks."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias, cast

from ...request_metadata.types import MetadataEntry

DiagnosticScalar: TypeAlias = None | bool | int | float | str | bytes
DiagnosticValue: TypeAlias = (
    DiagnosticScalar
    | Mapping[str, "DiagnosticValue"]
    | Sequence["DiagnosticValue"]
)


class DiagnosticSeverity(str, Enum):
    """Stable diagnostic severities used by both output formats."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    STATUS = "status"


def _validate_span_position(name: str, value: object) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 1
    ):
        raise ValueError(f"diagnostic span {name} must be positive")


def _validate_span_offset(name: str, value: object) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(
            f"diagnostic span {name} must be a non-negative integer"
        )


def _validate_span_bounds(
    text: str | None, start: int | None, end: int | None
) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError("diagnostic span start must not exceed end")
    if text is None:
        return
    if start is not None and start > len(text):
        raise ValueError("diagnostic span start exceeds its text")
    if end is not None and end > len(text):
        raise ValueError("diagnostic span end exceeds its text")


@dataclass(frozen=True)
class DiagnosticSpan:
    """A source location and optional display span."""

    source: str
    line: int | None = None
    column: int | None = None
    text: str | None = None
    start: int | None = None
    end: int | None = None
    label: str = ""

    def __post_init__(self) -> None:
        source = cast(object, self.source)
        text = cast(object, self.text)
        label = cast(object, self.label)
        if not isinstance(source, str) or not source:
            raise TypeError("diagnostic span source must be a non-empty string")
        _validate_span_position("line", cast(object, self.line))
        _validate_span_position("column", cast(object, self.column))
        if text is not None and not isinstance(text, str):
            raise TypeError("diagnostic span text must be a string")
        _validate_span_offset("start", cast(object, self.start))
        _validate_span_offset("end", cast(object, self.end))
        _validate_span_bounds(self.text, self.start, self.end)
        if not isinstance(label, str):
            raise TypeError("diagnostic span label must be a string")


@dataclass(frozen=True)
class DiagnosticDefinition:
    """Stable registry entry shared by human and machine diagnostics."""

    code: str
    event: str
    default_headline: str
    exit_class: int


DIAGNOSTIC_REGISTRY: Mapping[str, DiagnosticDefinition] = {
    definition.code: definition
    for definition in (
        DiagnosticDefinition(
            "E1001", "usage.invalid", "command-line arguments are invalid", 2
        ),
        DiagnosticDefinition(
            "E1002",
            "usage.invalid_diagnostic_format",
            "diagnostic format is invalid",
            2,
        ),
        DiagnosticDefinition(
            "E2001", "command.failed", "the command could not be completed", 1
        ),
        DiagnosticDefinition(
            "E2002",
            "schema.input_invalid",
            "a local schema input is invalid",
            1,
        ),
        DiagnosticDefinition(
            "E2004", "schema.symbol_not_found", "schema symbol was not found", 1
        ),
        DiagnosticDefinition(
            "E2101", "input.invalid", "request body is not valid input", 1
        ),
        DiagnosticDefinition(
            "E3002", "transport.failed", "could not connect to the target", 1
        ),
        DiagnosticDefinition(
            "E4103", "rpc.failed", "the RPC returned a failure status", 1
        ),
        DiagnosticDefinition(
            "E8001",
            "dependency.unavailable",
            "a runtime dependency is unavailable",
            1,
        ),
        DiagnosticDefinition(
            "E8002", "local_io.failed", "a local I/O operation failed", 1
        ),
        DiagnosticDefinition(
            "E9001",
            "internal.failed",
            "an unexpected internal failure occurred",
            1,
        ),
        DiagnosticDefinition(
            "W3001",
            "transport.insecure",
            "TLS certificate verification is disabled",
            0,
        ),
        DiagnosticDefinition(
            "W2001",
            "schema.compiler_warning",
            "the proto compiler reported a warning",
            0,
        ),
        DiagnosticDefinition(
            "W4101",
            "rpc.completed",
            "the RPC ended with a non-OK status",
            0,
        ),
    )
}

# E8002 describes local I/O failures. Descriptor export has its own stable
# event name so machine consumers can distinguish writes from input reads.
DIAGNOSTIC_EVENT_ALIASES: Mapping[str, frozenset[str]] = {
    "E8002": frozenset({"descriptor.export_failed"}),
}


def _empty_fields() -> dict[str, DiagnosticValue]:
    return {}


def _empty_events() -> list[DiagnosticEvent]:
    return []


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _is_nonnegative_number(value: object) -> bool:
    return _is_number(value) and cast(int | float, value) >= 0


def _is_nonnegative_integer(value: object) -> bool:
    return _is_integer(value) and cast(int, value) >= 0


def _is_positive_integer(value: object) -> bool:
    return _is_integer(value) and cast(int, value) >= 1


def _is_status(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    if set(mapping) != {"name", "number"}:
        return False
    number = mapping["number"]
    return _is_string(mapping["name"]) and (
        number is None or _is_integer(number)
    )


def _is_source(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    return (
        set(mapping) == {"path", "argument"}
        and _is_string(mapping["path"])
        and _is_string(mapping["argument"])
    )


def _is_sources(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    return all(_is_source(item) for item in cast(Sequence[object], value))


_ContextCheck: TypeAlias = tuple[str, Callable[[object], bool]]
_CONTEXT_SCHEMAS: Mapping[
    str, tuple[frozenset[str], Mapping[str, _ContextCheck]]
] = {
    "usage.invalid": (frozenset(), {}),
    "usage.invalid_diagnostic_format": (frozenset(), {}),
    "command.failed": (
        frozenset(),
        {
            "kind": ("a string", _is_string),
            "path": ("a string", _is_string),
            "argument": ("a string", _is_string),
            "sources": ("a sequence of option/path objects", _is_sources),
        },
    ),
    "schema.input_invalid": (frozenset(), {}),
    "schema.symbol_not_found": (frozenset(), {}),
    "input.invalid": (
        frozenset(),
        {
            "kind": ("a string", _is_string),
            "request_type": ("a string", _is_string),
            "request_index": ("a positive integer", _is_positive_integer),
        },
    ),
    "transport.failed": (frozenset(), {}),
    "dependency.unavailable": (frozenset(), {}),
    "internal.failed": (frozenset(), {}),
    "schema.compiler_warning": (frozenset(), {}),
    "local_io.failed": (
        frozenset({"operation"}),
        {
            "operation": ("a string", _is_string),
            "path": ("a string", _is_string),
            "argument": ("a string", _is_string),
            "stream": ("a string", _is_string),
            "errno": ("an integer", _is_integer),
        },
    ),
    "descriptor.export_failed": (
        frozenset({"operation", "path", "argument"}),
        {
            "operation": ("a string", _is_string),
            "path": ("a string", _is_string),
            "argument": ("a string", _is_string),
            "errno": ("an integer", _is_integer),
        },
    ),
    "transport.insecure": (
        frozenset(),
        {"target": ("a string", _is_string)},
    ),
    "transport.connecting": (
        frozenset({"target", "transport"}),
        {
            "target": ("a string", _is_string),
            "transport": ("a string", _is_string),
        },
    ),
    "transport.connected": (
        frozenset({"target"}),
        {"target": ("a string", _is_string)},
    ),
    "schema.source": (
        frozenset({"source"}),
        {"source": ("a string", _is_string)},
    ),
    "reflection.negotiated": (
        frozenset({"version"}),
        {"version": ("a string", _is_string)},
    ),
    "rpc.resolved": (
        frozenset({"method", "cardinality", "request_type", "response_type"}),
        {
            "method": ("a string", _is_string),
            "cardinality": ("a string", _is_string),
            "request_type": ("a string", _is_string),
            "response_type": ("a string", _is_string),
        },
    ),
    "rpc.started": (
        frozenset(),
        {
            "method": ("a string", _is_string),
            "compression": ("a string", _is_string),
            "target": ("a string", _is_string),
        },
    ),
    "rpc.lifecycle.completed": (
        frozenset({"sent", "received"}),
        {
            "sent": ("a non-negative integer", _is_nonnegative_integer),
            "received": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
        },
    ),
    "rpc.completed": (
        frozenset({"status", "sent", "received"}),
        {
            "status": ("a string", _is_string),
            "grpc_status": ("a status object", _is_status),
            "elapsed_seconds": (
                "a finite non-negative number",
                _is_nonnegative_number,
            ),
            "sent": ("a non-negative integer", _is_nonnegative_integer),
            "received": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
            "sent_messages": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
            "received_messages": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
        },
    ),
    "rpc.failed": (
        frozenset(),
        {
            "target": ("a string", _is_string),
            "method": ("a string", _is_string),
            "request_type": ("a string", _is_string),
            "status": ("a string", _is_string),
            "grpc_status": ("a status object", _is_status),
            "rich_status": ("a status object", _is_status),
            "elapsed_seconds": (
                "a finite non-negative number",
                _is_nonnegative_number,
            ),
            "sent": ("a non-negative integer", _is_nonnegative_integer),
            "received": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
            "sent_messages": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
            "received_messages": (
                "a non-negative integer",
                _is_nonnegative_integer,
            ),
        },
    ),
    "rpc.headers": (frozenset(), {}),
    "rpc.trailers": (frozenset(), {}),
}


def _validate_value(
    value: object, path: str, active: set[int] | None = None
) -> None:
    """Reject values that cannot be represented by diagnostic schema v1."""
    if value is None or isinstance(value, (bool, int, str, bytes)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain non-finite numbers")
        return
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise ValueError(f"{path} must not contain a cycle")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            for key, item in mapping.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} keys must be strings")
                _validate_value(item, f"{path}.{key}", active)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            sequence = cast(Sequence[object], value)
            for index, item in enumerate(sequence):
                _validate_value(item, f"{path}[{index}]", active)
            return
    finally:
        active.remove(identity)
    raise TypeError(
        f"{path} has unsupported diagnostic value type {type(value).__name__}"
    )


def _validate_context(name: str, fields: Mapping[str, DiagnosticValue]) -> None:
    schema = _CONTEXT_SCHEMAS.get(name)
    if schema is None:
        return
    required, checks = schema
    unknown = sorted(set(fields) - set(checks))
    if unknown:
        rendered = ", ".join(repr(key) for key in unknown)
        raise ValueError(
            f"diagnostic event {name!r} has unknown context key(s): {rendered}"
        )
    missing = sorted(required - set(fields))
    if missing:
        rendered = ", ".join(repr(key) for key in missing)
        raise ValueError(
            f"diagnostic event {name!r} is missing context key(s): {rendered}"
        )
    for key in sorted(fields):
        description, predicate = checks[key]
        if not predicate(fields[key]):
            raise TypeError(
                f"diagnostic event {name!r} context {key!r} must be "
                f"{description}"
            )


def _validate_event_header(
    name: object, message: object, level: object
) -> None:
    if not isinstance(name, str) or not name:
        raise TypeError("diagnostic event name must be a non-empty string")
    if not isinstance(message, str):
        raise TypeError("diagnostic event message must be a string")
    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("diagnostic event level must be an integer")


def _validate_metadata(value: object) -> None:
    if not isinstance(value, tuple):
        raise TypeError("diagnostic metadata must be a tuple")
    for entry in cast(tuple[object, ...], value):
        if not isinstance(entry, tuple):
            raise TypeError(
                "diagnostic metadata entries must be (str, str | bytes) tuples"
            )
        typed_entry = cast(tuple[object, ...], entry)
        if (
            len(typed_entry) != 2
            or not isinstance(typed_entry[0], str)
            or not isinstance(typed_entry[1], (str, bytes))
        ):
            raise TypeError(
                "diagnostic metadata entries must be (str, str | bytes) tuples"
            )


def _validate_spans(value: object) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(span, DiagnosticSpan)
        for span in cast(tuple[object, ...], value)
    ):
        raise TypeError("diagnostic spans must contain DiagnosticSpan values")


def _validate_string_tuple(name: str, value: object) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) for item in cast(tuple[object, ...], value)
    ):
        raise TypeError(f"diagnostic {name} must contain strings")


@dataclass(frozen=True)
class DiagnosticEvent:
    """A structured diagnostic event independent of its rendering."""

    name: str
    message: str = ""
    level: int = 1
    fields: Mapping[str, DiagnosticValue] = field(default_factory=_empty_fields)
    metadata: tuple[MetadataEntry, ...] = ()
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    code: str | None = None
    spans: tuple[DiagnosticSpan, ...] = ()
    causes: tuple[str, ...] = ()
    help: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    details: tuple[DiagnosticValue, ...] = ()

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate the public event and its schema-v1 representation."""
        _validate_event_header(
            cast(object, self.name),
            cast(object, self.message),
            cast(object, self.level),
        )

        fields_value = cast(object, self.fields)
        if not isinstance(fields_value, Mapping):
            raise TypeError("diagnostic event fields must be a mapping")
        _validate_value(cast(object, fields_value), "diagnostic context")

        _validate_metadata(cast(object, self.metadata))
        if not isinstance(cast(object, self.severity), DiagnosticSeverity):
            raise TypeError("diagnostic severity must be DiagnosticSeverity")
        if self.code is not None and not isinstance(
            cast(object, self.code), str
        ):
            raise TypeError("diagnostic code must be a string")

        _validate_spans(cast(object, self.spans))
        for name, values in (
            ("causes", self.causes),
            ("help", self.help),
            ("notes", self.notes),
        ):
            _validate_string_tuple(name, cast(object, values))

        details_value = cast(object, self.details)
        if not isinstance(details_value, tuple):
            raise TypeError("diagnostic details must be a tuple")
        _validate_value(cast(object, details_value), "diagnostic details")
        _validate_context(self.name, self.fields)


class DiagnosticSink(Protocol):
    """Consume structured diagnostic events."""

    def emit(self, event: DiagnosticEvent) -> None:
        """Consume one diagnostic event."""
        ...


class NullDiagnosticSink:
    """No-op sink used when diagnostics are disabled."""

    def emit(self, event: DiagnosticEvent) -> None:
        """Validate and discard a diagnostic event."""
        event.validate()


@dataclass
class RecordingDiagnosticSink:
    """A sink intended for tests and programmatic consumers."""

    events: list[DiagnosticEvent] = field(default_factory=_empty_events)

    def emit(self, event: DiagnosticEvent) -> None:
        """Validate and append a diagnostic event to the recording."""
        event.validate()
        self.events.append(event)
