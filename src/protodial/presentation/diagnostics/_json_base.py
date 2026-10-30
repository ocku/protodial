"""Bound contextual JSON fields while retaining schema-v1 structure."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import islice
from typing import cast

from ._json import (
    json_object,
)
from ._json_values import (
    compact_json,
    span_json,
)
from ._messages import (
    event_message,
)
from ._render_policy import (
    SCHEMA,
    JsonTruncationMetrics,
)
from ._terminal import (
    abbreviate_cells,
    abbreviate_prefix,
)
from .models import (
    DiagnosticEvent,
)


def _bounded_json_value(
    value: object, string_limit: int, collection_limit: int = 16
) -> object:
    """Bound attacker-controlled strings while retaining JSON value shapes."""
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, str):
        return abbreviate_prefix(
            value,
            max(1, string_limit),
            max_bytes=max(3, string_limit * 4),
            max_codepoints=max(1, string_limit),
        )
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            abbreviate_cells(
                str(key), 128, max_bytes=512, max_codepoints=128
            ): _bounded_json_value(item, string_limit, collection_limit)
            for key, item in islice(mapping.items(), collection_limit)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence = cast(Sequence[object], value)
        return [
            _bounded_json_value(item, string_limit, collection_limit)
            for item in islice(sequence, collection_limit)
        ]
    return value


def minimal_json(event: DiagnosticEvent, sequence: int, limit: int) -> str:
    """Build a valid bounded event, retaining RPC essentials when present."""
    essential_names = {
        "elapsed_seconds",
        "grpc_status",
        "method",
        "received",
        "received_messages",
        "sent",
        "sent_messages",
        "status",
        "target",
    }
    if event.name in {"rpc.completed", "rpc.failed"}:
        context = {
            key: value
            for key, value in event.fields.items()
            if key in essential_names
        }
    else:
        context = dict(islice(event.fields.items(), 8))

    string_limit = 2048
    while string_limit >= 16:
        value: dict[str, object] = {
            "schema": SCHEMA,
            "sequence": sequence,
            "severity": event.severity.value,
            "event": _bounded_json_value(event.name, min(256, string_limit)),
            "message": _bounded_json_value(
                event_message(event, sanitize=False), string_limit
            ),
            "notes": ["diagnostic truncated to fit the 64 KiB sink limit"],
        }
        if event.code:
            value["code"] = _bounded_json_value(event.code, 64)
        if context:
            value["context"] = _bounded_json_value(context, string_limit)
        if event.causes:
            value["causes"] = _bounded_json_value(
                event.causes[:4], string_limit
            )
        rendered = compact_json(value)
        if len(rendered) <= limit:
            return rendered
        string_limit //= 2

    minimal = compact_json(
        {
            "schema": SCHEMA,
            "sequence": sequence,
            "severity": event.severity.value,
            "event": "diagnostic.truncated",
            "message": "diagnostic truncated",
        }
    )
    if len(minimal) <= limit:
        return minimal
    raise ValueError("diagnostic output limit is too small for the JSON schema")


def bounded_diagnostic_json_base(
    event: DiagnosticEvent,
    sequence: int,
    budget: int,
    *,
    _metrics: JsonTruncationMetrics | None = None,
) -> dict[str, object]:
    """Bound non-detail content before complete details compete for space."""

    generic_note = "diagnostic truncated to fit the 64 KiB sink limit"

    def collections_fit(value: object, collection_limit: int) -> bool:
        """Check bounded shape without descending into discarded entries."""
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            return len(mapping) <= collection_limit and all(
                collections_fit(item, collection_limit)
                for item in mapping.values()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            sequence = cast(Sequence[object], value)
            return len(sequence) <= collection_limit and all(
                collections_fit(item, collection_limit) for item in sequence
            )
        return True

    # A raw candidate is itself an eight-entry collection candidate. Do not
    # walk a large extension context only to throw entries 9..N away on the
    # immediately following bounded attempt.
    if collections_fit(event.fields, 8):
        if _metrics is not None:
            _metrics.base_candidates += 1
            _metrics.context_entries += len(event.fields)
        raw = json_object(replace(event, details=()), sequence)
        if len(compact_json(raw).encode("utf-8")) <= budget:
            return raw

    def build(string_limit: int, collection_limit: int) -> dict[str, object]:
        if _metrics is not None:
            _metrics.base_candidates += 1
        value: dict[str, object] = {
            "schema": SCHEMA,
            "sequence": sequence,
            "severity": event.severity.value,
            "event": _bounded_json_value(
                event.name, min(256, string_limit), collection_limit
            ),
            "message": _bounded_json_value(
                event_message(event, sanitize=False),
                string_limit,
                collection_limit,
            ),
        }
        if event.code:
            value["code"] = _bounded_json_value(
                event.code, min(64, string_limit), collection_limit
            )
        if event.fields:
            # Keep each surviving key independently bounded so a large
            # contextual identifier cannot hide later values. ``islice`` is
            # important here: discarded extension entries must not be
            # normalized, serialized, or otherwise inspected.
            context: dict[str, object] = {}
            for key, item in islice(event.fields.items(), collection_limit):
                if _metrics is not None:
                    _metrics.context_entries += 1
                context[
                    abbreviate_cells(
                        str(key), 128, max_bytes=512, max_codepoints=128
                    )
                ] = _bounded_json_value(item, string_limit, collection_limit)
            value["context"] = context
        if event.spans:
            value["spans"] = [
                _bounded_json_value(
                    span_json(span), string_limit, collection_limit
                )
                for span in event.spans[:collection_limit]
            ]
        if event.causes:
            value["causes"] = [
                _bounded_json_value(item, string_limit, collection_limit)
                for item in event.causes[:collection_limit]
            ]
        if event.help:
            value["help"] = [
                _bounded_json_value(item, string_limit, collection_limit)
                for item in event.help[:collection_limit]
            ]
        notes = [
            _bounded_json_value(item, string_limit, collection_limit)
            for item in event.notes[:collection_limit]
        ]
        notes.append(generic_note)
        value["notes"] = notes
        if event.metadata:
            value["metadata"] = [
                {
                    "name": _bounded_json_value(
                        name, string_limit, collection_limit
                    ),
                    "value": _bounded_json_value(
                        raw_value,
                        string_limit,
                        collection_limit,
                    ),
                }
                for name, raw_value in event.metadata[:collection_limit]
            ]
        return value

    for collection_limit in (8, 4, 2, 1):
        string_limit = 2048
        while string_limit >= 16:
            candidate = build(string_limit, collection_limit)
            if len(compact_json(candidate).encode("utf-8")) <= budget:
                return candidate
            string_limit //= 2

    # The validated RPC context is small enough for the loop above at the
    # production limit. Retain the established schema-valid fallback for
    # deliberately tiny private-call limits.
    return cast(
        dict[str, object],
        json.loads(minimal_json(event, sequence, budget)),
    )
