"""Rich-detail identity, ordering, and value presentation."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import cast

from ..._rich_details import standard_detail_name
from ...request_metadata.types import Metadata
from ._json_values import (
    json_value,
)
from ._terminal import (
    abbreviate_cells,
    detail_identity,
    detail_prefix_budget,
    display_width,
    wrap_cells,
)
from ._text import (
    safe,
    style,
)
from .models import (
    DiagnosticValue,
)

_RICH_DETAIL_PRIORITY: Mapping[str, int] = {
    "BadRequest": 0,
    "PreconditionFailure": 0,
    "QuotaFailure": 0,
    "ErrorInfo": 1,
    "ResourceInfo": 1,
    "RetryInfo": 1,
    "RequestInfo": 1,
    "Help": 1,
    "LocalizedMessage": 1,
    "DebugInfo": 2,
}

_UNKNOWN_RICH_DETAIL_PRIORITY = 3


def rich_detail_priority(detail: DiagnosticValue) -> int:
    """Return the documented human/truncation priority of one detail."""
    if not isinstance(detail, Mapping):
        return _UNKNOWN_RICH_DETAIL_PRIORITY
    return _RICH_DETAIL_PRIORITY.get(
        detail_name(detail), _UNKNOWN_RICH_DETAIL_PRIORITY
    )


def detail_warning_text(
    detail: Mapping[str, DiagnosticValue], width: int
) -> str:
    """Render a decode warning without repeating titled custom identity."""
    warning = safe(detail["warning"])
    type_name = detail_identity(detail.get("type", "unknown"), width)
    raw_title = detail.get("title")
    if (
        not detail_name(detail)
        and has_visible_detail_value(raw_title)
        and detail_identity(raw_title, width) != type_name
    ):
        return warning
    return f"`{type_name}` detail: {warning}"


def prioritized_rich_details(
    details: Sequence[DiagnosticValue],
) -> list[tuple[int, DiagnosticValue]]:
    """Return indexed details in stable semantic presentation order."""
    buckets: list[list[tuple[int, DiagnosticValue]]] = [
        [] for _ in range(_UNKNOWN_RICH_DETAIL_PRIORITY + 1)
    ]
    for item in enumerate(details):
        buckets[rich_detail_priority(item[1])].append(item)
    return [item for bucket in buckets for item in bucket]


def detail_name(detail: Mapping[str, DiagnosticValue]) -> str:
    """Return a recognized standard detail name, or an empty string."""
    return standard_detail_name(detail.get("type", "detail")) or ""


def has_visible_detail_value(value: object) -> bool:
    """Return whether a known detail value adds visible information."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(has_visible_detail_value(item) for item in mapping.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence = cast(Sequence[object], value)
        return any(has_visible_detail_value(item) for item in sequence)
    return bool(value)


def detail_value_lines(
    prefix: str,
    value: object,
    *,
    width: int,
    styled_prefix: str | None = None,
) -> list[str]:
    """Wrap a detail value with a stable, readable hanging indent."""
    safe_value = safe(value)
    bounded_prefix = abbreviate_cells(prefix, detail_prefix_budget(width))
    prefix_width = display_width(bounded_prefix)
    available = max(1, width - prefix_width)
    wrapped = wrap_cells(safe_value, available)
    plain = [
        bounded_prefix + wrapped[0],
        *(" " * prefix_width + line for line in wrapped[1:]),
    ]
    if any(display_width(line) > width for line in plain):
        # A value can begin with a wide grapheme that does not fit in the
        # prefix's remaining cells. Reflow the complete plain line instead.
        return wrap_cells(bounded_prefix + safe_value, max(1, width))
    if styled_prefix is not None:
        plain[0] = (
            styled_prefix + wrapped[0]
            if bounded_prefix == prefix
            else (
                style(bounded_prefix, "strong", True)
                if "\x1b" in styled_prefix
                else bounded_prefix
            )
            + wrapped[0]
        )
    return plain


def render_value(value: DiagnosticValue) -> str:
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    metadata = _metadata_value(value)
    if metadata is not None:
        return ", ".join(
            f"{abbreviate_cells(safe(key), 128)}={render_value(item)}"
            for key, item in metadata
        )
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Mapping):
        return json.dumps(
            _human_json_value(value), ensure_ascii=True, sort_keys=True
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return json.dumps(json_value(value), ensure_ascii=True)
    return safe(value)


def _human_json_value(value: DiagnosticValue) -> object:
    """Convert nested human detail data to JSON-compatible values."""

    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    if isinstance(value, Mapping):
        return {
            str(key): _human_json_value(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_human_json_value(item) for item in value]
    return value


def _metadata_value(value: object) -> Metadata | None:
    """Narrow an arbitrary diagnostic value to ordered gRPC metadata."""

    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return None
    entries = cast(Sequence[object], value)
    metadata: Metadata = []
    for item in entries:
        if not isinstance(item, Sequence) or isinstance(
            item, (str, bytes, bytearray)
        ):
            return None
        entry = cast(Sequence[object], item)
        if len(entry) != 2:
            return None
        key, entry_value = entry
        if not isinstance(key, str) or not isinstance(
            entry_value, (str, bytes)
        ):
            return None
        metadata.append((key, entry_value))
    return metadata
