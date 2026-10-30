"""Define presentation-neutral diagnostic events and sinks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast

from ...request_metadata.types import MetadataEntry
from ._value_validation import (
    DiagnosticScalar as DiagnosticScalar,
)
from ._value_validation import (
    DiagnosticValue as DiagnosticValue,
)
from ._value_validation import (
    validate_context,
    validate_value,
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
        validate_value(cast(object, fields_value), "diagnostic context")

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
        validate_value(cast(object, details_value), "diagnostic details")
        validate_context(self.name, self.fields)


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
