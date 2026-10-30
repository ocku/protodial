"""Serialize complete diagnostic events as schema-v1 JSON."""

from __future__ import annotations

from typing import cast

from ._json_values import (
    compact_json,
    json_value,
    span_json,
)
from ._messages import (
    event_message,
)
from ._render_policy import (
    SCHEMA,
)
from .models import (
    DiagnosticEvent,
)


def json_object(event: DiagnosticEvent, sequence: int) -> dict[str, object]:
    """Build one schema-v1 object before compact serialization."""
    sequence_value = cast(object, sequence)
    if (
        isinstance(sequence_value, bool)
        or not isinstance(sequence_value, int)
        or sequence_value < 1
    ):
        raise ValueError("diagnostic sequence must be a positive integer")
    value: dict[str, object] = {
        "schema": SCHEMA,
        "sequence": sequence,
        "severity": event.severity.value,
        "event": event.name,
        "message": event_message(event, sanitize=False),
    }
    if event.code:
        value["code"] = event.code
    if event.fields:
        value["context"] = json_value(event.fields)
    if event.spans:
        value["spans"] = [span_json(span) for span in event.spans]
    if event.causes:
        value["causes"] = list(event.causes)
    if event.help:
        value["help"] = list(event.help)
    if event.notes:
        value["notes"] = list(event.notes)
    if event.metadata:
        metadata: list[dict[str, object]] = []
        for name, raw in event.metadata:
            metadata.append({"name": name, "value": json_value(raw)})
        value["metadata"] = metadata
    if event.details:
        value["details"] = json_value(event.details)
    return value


def render_json(event: DiagnosticEvent, sequence: int) -> str:
    """Render one event as compact schema-v1 JSON."""
    event.validate()
    return compact_json(json_object(event, sequence))
