"""Diagnostic values and schema-v1 context validation."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import TypeAlias, cast

DiagnosticScalar: TypeAlias = None | bool | int | float | str | bytes

DiagnosticValue: TypeAlias = (
    DiagnosticScalar
    | Mapping[str, "DiagnosticValue"]
    | Sequence["DiagnosticValue"]
)


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


def validate_value(
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
                validate_value(item, f"{path}.{key}", active)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            sequence = cast(Sequence[object], value)
            for index, item in enumerate(sequence):
                validate_value(item, f"{path}[{index}]", active)
            return
    finally:
        active.remove(identity)
    raise TypeError(
        f"{path} has unsupported diagnostic value type {type(value).__name__}"
    )


def validate_context(name: str, fields: Mapping[str, DiagnosticValue]) -> None:
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
