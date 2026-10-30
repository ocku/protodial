"""Normalize diagnostic values and encode compact JSON."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping, Sequence

from .models import (
    DiagnosticSpan,
    DiagnosticValue,
)


def json_value(value: DiagnosticValue) -> object:
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("diagnostic JSON cannot contain non-finite numbers")
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [json_value(item) for item in value]
    return value


def span_json(span: DiagnosticSpan) -> dict[str, object]:
    value: dict[str, object] = {"source": span.source}
    for key in ("line", "column", "text", "start", "end", "label"):
        item = getattr(span, key)
        if item is not None and item != "":
            value[key] = item
    return value


def compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
