"""Render structured diagnostics as human text or NDJSON."""

from __future__ import annotations

import base64
import bisect
import json
import math
import os
import re
import sys
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from itertools import islice
from typing import TextIO, cast

from ..._rich_details import standard_detail_name
from ...request_metadata.types import Metadata
from .._graphemes import (
    enclosing_grapheme_span,
    grapheme_clusters,
)
from .._truncation import (
    PrefixSearchMetrics,
    abbreviate_graphemes,
    truncate_with_marker,
)
from ..sanitization import escape_control_characters
from .models import (
    DIAGNOSTIC_EVENT_ALIASES,
    DIAGNOSTIC_REGISTRY,
    DiagnosticEvent,
    DiagnosticSpan,
    DiagnosticValue,
)

SCHEMA = "protodial.diagnostic.v1"
MAX_RENDERED_DIAGNOSTIC_CHARS = 64 * 1024
MAX_RICH_DETAIL_PREVIEW_CHARS = 8 * 1024
MAX_TRUNCATED_JSON_NON_DETAIL_CHARS = 16 * 1024

# Human presentation is intentionally not wire order.  Corrective information
# that can tell a caller what to change leads supporting request/resource
# context, which in turn leads debugging and opaque bulk data.  Python's sort
# is stable, so equal-priority detail types preserve their wire order.  NDJSON
# remains raw wire order unless it must be truncated; the truncator uses this
# same policy only to decide which complete objects survive the byte budget.
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


class DiagnosticFormat(str, Enum):
    """Supported diagnostic stream encodings."""

    HUMAN = "human"
    JSON = "json"


class ColorPolicy(str, Enum):
    """Supported human diagnostic color policies."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


@dataclass
class JsonTruncationMetrics:
    """Deterministic work counters for bounded-JSON complexity tests."""

    base_candidates: int = 0
    context_entries: int = 0
    normalized_details: int = 0
    serialized_details: int = 0
    selection_candidates: int = 0
    final_detail_scans: int = 0
    preview_candidates: int = 0


@dataclass
class HumanTruncationMetrics:
    """Deterministic work counters for bounded-human complexity tests."""

    rendered_groups: int = 0
    selection_candidates: int = 0
    partial_line_candidates: int = 0
    final_group_scans: int = 0


@dataclass(frozen=True)
class _DetailTruncationItem:
    """One semantic rich-detail item plus its optional shared heading."""

    text: str
    heading: str = ""


_STYLE = {
    "error": "1;31",
    "warning": "1;33",
    "info": "1;36",
    "status": "1;32",
    "location": "34",
    "gutter": "2",
    "help": "36",
    "strong": "1",
}


def _style(text: str, role: str, enabled: bool) -> str:
    return f"\x1b[{_STYLE[role]}m{text}\x1b[0m" if enabled else text


def _safe(value: object) -> str:
    return escape_control_characters(value)


def format_duration(seconds: float) -> str:
    """Format seconds with a compact, three-significant-digit unit."""
    if not math.isfinite(seconds) or seconds < 0:
        return "unknown duration"
    # Select the unit after accounting for three-significant-digit rounding.
    # Otherwise 999.5 us would be rendered as the contradictory ``1e+03 us``
    # instead of being promoted to ``1 ms`` (and likewise for milliseconds).
    if seconds < 0.0009995:
        return f"{_three_significant_digits(seconds * 1_000_000)} us"
    if seconds < 0.001:
        return "1 ms"
    if seconds < 0.9995:
        return f"{_three_significant_digits(seconds * 1000)} ms"
    if seconds < 1:
        return "1 s"
    return f"{_three_significant_digits(seconds)} s"


def _three_significant_digits(value: float) -> str:
    """Render a finite value with three significant digits and no exponent."""
    if value == 0:
        return "0"
    magnitude = math.floor(math.log10(abs(value)))
    decimal_places = max(0, 2 - magnitude)
    rendered = f"{value:.{decimal_places}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _count(value: int) -> str:
    return f"{value} message" + ("" if value == 1 else "s")


def _message_or(event: DiagnosticEvent, generated: str, sanitize: bool) -> str:
    """Prefer explicit prose when the generated headline lacks its subject."""
    if event.message:
        return _safe(event.message) if sanitize else event.message
    return generated


def _duration_suffix(value: DiagnosticValue | None) -> str:
    """Render an optional numeric duration as a headline suffix."""
    if isinstance(value, (int, float)):
        return f" in {format_duration(float(value))}"
    return ""


def _message_count(fields: Mapping[str, DiagnosticValue], name: str) -> int:
    """Read an integer message counter with a safe default."""
    value = fields.get(name, 0)
    return (
        value if isinstance(value, int) and not isinstance(value, bool) else 0
    )


def _counter(
    fields: Mapping[str, DiagnosticValue], canonical: str, legacy: str
) -> int | None:
    """Read a counter from either additive schema-v1 spelling."""
    for name in (canonical, legacy):
        value = fields.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _status_parts(event: DiagnosticEvent) -> tuple[str, int | None]:
    """Read an RPC status from canonical or compatibility context fields."""
    status = event.fields.get("grpc_status")
    if isinstance(status, Mapping):
        raw_name = status.get("name", "UNKNOWN")
        name = str(raw_name)
        raw_number = status.get("number")
        number = (
            raw_number
            if isinstance(raw_number, int) and not isinstance(raw_number, bool)
            else None
        )
        return name, number
    raw_name = event.fields.get("status", "UNKNOWN")
    return str(raw_name), None


def _connecting_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render a connection-attempt headline."""
    target = _safe(event.fields.get("target", ""))
    transport = _safe(event.fields.get("transport", ""))
    suffix = f" using {transport} transport" if transport else ""
    return f"connecting to `{target}`{suffix}"


def _connected_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render a successful connection headline."""
    target = _safe(event.fields.get("target", ""))
    return f"connected to `{target}`"


def _schema_source_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render the selected schema source."""
    source = _safe(event.fields.get("source", "schema"))
    return f"using {source} schema"


def _reflection_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render the negotiated reflection protocol."""
    version = _safe(event.fields.get("version", ""))
    return f"server reflection negotiated {version}"


def _resolved_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render a resolved RPC method and its type signature."""
    method = _safe(event.fields.get("method", ""))
    if not method:
        return _message_or(event, "resolved RPC method", sanitize)
    cardinality = _safe(event.fields.get("cardinality", ""))
    request_type = _safe(event.fields.get("request_type", ""))
    response_type = _safe(event.fields.get("response_type", ""))
    signature = (
        f"; `{request_type}` -> `{response_type}`"
        if request_type and response_type
        else ""
    )
    shape = f" ({cardinality}{signature})" if cardinality else ""
    return f"resolved `{method}`{shape}"


def _started_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render an RPC-start headline and compression mode."""
    method = _safe(event.fields.get("method", ""))
    if not method:
        return _message_or(event, "calling RPC method", sanitize)
    compression = _safe(event.fields.get("compression", ""))
    if compression == "none":
        suffix = " without compression"
    elif compression:
        suffix = f" using {compression} compression"
    else:
        suffix = ""
    return f"calling `{method}`{suffix}"


def _lifecycle_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render internal RPC lifecycle counters."""
    sent = _message_count(event.fields, "sent")
    received = _message_count(event.fields, "received")
    return (
        f"RPC lifecycle completed; sent {_count(sent)}; "
        f"received {_count(received)}"
    )


def _completed_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render final status, timing, and message counters."""
    status_name, number = _status_parts(event)
    status = _safe(status_name) if sanitize else status_name
    if status != "OK" and number is not None:
        status = f"{status} ({number})"
    timing = _duration_suffix(event.fields.get("elapsed_seconds"))
    sent = _counter(event.fields, "sent", "sent_messages") or 0
    received = _counter(event.fields, "received", "received_messages") or 0
    return f"{status}{timing}; sent {_count(sent)}; received {_count(received)}"


def _failed_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Build stable machine prose even when a failure supplied no message."""
    if event.message:
        return _safe(event.message) if sanitize else event.message
    method_value = event.fields.get("method")
    method = str(method_value) if isinstance(method_value, str) else ""
    status_name, number = _status_parts(event)
    if sanitize:
        method = _safe(method)
        status_name = _safe(status_name)
    status = f"{status_name} ({number})" if number is not None else status_name
    subject = f"RPC `{method}`" if method else "RPC"
    return f"{subject} failed with {status}"


_EVENT_MESSAGE_BUILDERS: Mapping[
    str, Callable[[DiagnosticEvent, bool], str]
] = {
    "transport.connecting": _connecting_message,
    "transport.connected": _connected_message,
    "schema.source": _schema_source_message,
    "reflection.negotiated": _reflection_message,
    "rpc.resolved": _resolved_message,
    "rpc.started": _started_message,
    "rpc.lifecycle.completed": _lifecycle_message,
    "rpc.completed": _completed_message,
    "rpc.failed": _failed_message,
}


def _event_message(event: DiagnosticEvent, *, sanitize: bool = True) -> str:
    """Return semantic prose for known events without dumping fields."""
    builder = _EVENT_MESSAGE_BUILDERS.get(event.name)
    if builder is not None:
        return builder(event, sanitize)
    if event.message:
        return _safe(event.message) if sanitize else event.message
    generated = event.name.replace(".", " ")
    return _safe(generated) if sanitize else generated


def _style_semantic_text(text: str, color: bool) -> str:
    if not color:
        return text
    return re.sub(
        r"(`[^`]*`)",
        lambda match: _style(match.group(1), "strong", True),
        text,
    )


def _terminal_width(stream: TextIO) -> int:
    try:
        width = os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        width = 100
    return min(120, max(60, width))


def _cluster_width(cluster: str) -> int:
    widths: list[int] = []
    for character in cluster:
        if (
            unicodedata.category(character).startswith("M")
            or "\ufe00" <= character <= "\ufe0f"
            or "\U0001f3fb" <= character <= "\U0001f3ff"
            or character == "\u200d"
            or unicodedata.category(character).startswith("C")
        ):
            widths.append(0)
        else:
            widths.append(
                2
                if unicodedata.east_asian_width(character) in {"W", "F"}
                else 1
            )
    # Joined emoji, flags, and keycaps occupy one glyph cell group rather than
    # the sum of every component code point.
    if "\u200d" in cluster:
        return max(widths, default=0)
    if all("\U0001f1e6" <= item <= "\U0001f1ff" for item in cluster):
        return 2
    if "\u20e3" in cluster or "\ufe0f" in cluster:
        return max(2, max(widths, default=0))
    return sum(widths)


def _display_width(text: str) -> int:
    """Return terminal-cell width for unstyled, sanitized text."""
    return sum(_cluster_width(cluster) for cluster in grapheme_clusters(text))


def _abbreviate_cells(
    text: str,
    maximum: int,
    *,
    max_bytes: int = 1024,
    max_codepoints: int = 1024,
) -> str:
    """Middle-abbreviate text by cells and by invisible storage size."""
    return abbreviate_graphemes(
        text,
        max_cells=max(1, maximum),
        max_bytes=max(3, max_bytes),
        max_codepoints=max(1, max_codepoints),
        cluster_width=_cluster_width,
    )


def abbreviate_terminal_text(
    text: str, maximum: int, *, max_bytes: int = 1024
) -> str:
    """Bound a sanitized terminal identifier without splitting a glyph."""
    if maximum <= 0:
        raise ValueError("terminal text cell limit must be positive")
    if max_bytes <= 0:
        raise ValueError("terminal text byte limit must be positive")
    return abbreviate_graphemes(
        text,
        max_cells=maximum,
        max_bytes=max_bytes,
        max_codepoints=max_bytes,
        cluster_width=_cluster_width,
    )


def _abbreviate_prefix(
    text: str,
    maximum: int,
    *,
    max_bytes: int,
    max_codepoints: int,
) -> str:
    """End-abbreviate text with the same grapheme-safe dual bounds."""
    return abbreviate_graphemes(
        text,
        max_cells=max(1, maximum),
        max_bytes=max(3, max_bytes),
        max_codepoints=max(1, max_codepoints),
        cluster_width=_cluster_width,
        preserve_suffix=False,
    )


def _detail_prefix_budget(width: int) -> int:
    """Reserve at least half a normal detail line for its useful value."""
    return max(1, min(48, width // 2))


def _detail_identity(value: object, width: int) -> str:
    """Sanitize and independently bound contextual rich-detail text."""
    return _abbreviate_cells(_safe(value), _detail_prefix_budget(width))


def _wrap_cells(text: str, width: int) -> list[str]:
    """Wrap text by terminal cells while retaining combining sequences."""
    width = max(1, width)
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        word_width = _display_width(word)
        if current and _display_width(current) + 1 + word_width <= width:
            current += " " + word
            continue
        if current:
            lines.append(current)
            current = ""
        if word_width <= width:
            current = word
            continue

        chunk = ""
        chunk_width = 0
        for cluster in grapheme_clusters(word):
            cluster_width = _cluster_width(cluster)
            if chunk and chunk_width + cluster_width > width:
                lines.append(chunk)
                chunk = ""
                chunk_width = 0
            # A glyph can be wider than a deliberately tiny width. Keeping
            # its grapheme intact is preferable to corrupting it.
            chunk += cluster
            chunk_width += cluster_width
        current = chunk
    if current:
        lines.append(current)
    return lines or [""]


def terminal_cell_width(text: str) -> int:
    """Return the terminal width used by public presentation renderers."""
    return _display_width(text)


def wrap_terminal_cells(text: str, width: int) -> list[str]:
    """Wrap sanitized text using the shared terminal-cell policy."""
    return _wrap_cells(text, width)


def _span_window(
    text: str, highlight_start: int, width: int
) -> tuple[str, int, int, int]:
    """Crop *text* around a highlighted grapheme within a cell budget.

    Return the rendered window, its source-cell bounds, and the number of
    leading cells occupied by an ellipsis.  The highlighted grapheme is kept
    even when surrounding context and one or both ellipses must be dropped.
    """
    clusters = list(grapheme_clusters(text))
    if not clusters:
        return "", 0, 0, 0
    widths = [_cluster_width(cluster) for cluster in clusters]
    offsets = [0]
    for cluster_width in widths:
        offsets.append(offsets[-1] + cluster_width)

    target = bisect.bisect_right(offsets, highlight_start) - 1
    target = max(0, min(target, len(clusters) - 1))
    start = target
    end = target + 1

    def cost(candidate_start: int, candidate_end: int) -> int:
        markers = int(candidate_start > 0) + int(candidate_end < len(clusters))
        return offsets[candidate_end] - offsets[candidate_start] + markers

    # Source diagnostics benefit most from context before the caret. Expand
    # there first (up to 30 cells), then use all remaining space on either
    # side. Every expansion is grapheme-aligned and includes its ellipses.
    while start > 0 and highlight_start - offsets[start] < 30:
        if cost(start - 1, end) > width:
            break
        start -= 1
    while end < len(clusters) and cost(start, end + 1) <= width:
        end += 1
    while start > 0 and cost(start - 1, end) <= width:
        start -= 1

    content_width = offsets[end] - offsets[start]
    left_marker = "…" if start > 0 else ""
    right_marker = "…" if end < len(clusters) else ""
    # Tiny widths can fit the important glyph but not both crop markers.
    while content_width + len(left_marker) + len(right_marker) > width:
        if right_marker:
            right_marker = ""
        elif left_marker:
            left_marker = ""
        else:
            break
    shown = left_marker + "".join(clusters[start:end]) + right_marker
    return shown, offsets[start], offsets[end], len(left_marker)


def _span_location_lines(
    location: str, *, color: bool, width: int
) -> list[str]:
    """Render a source location without allowing it to exceed *width*."""
    # Location is context for the actionable source excerpt and caret. Keep it
    # compact even when one displayed glyph has many combining marks.
    location = _abbreviate_cells(
        location,
        max(1, width * 3),
        max_bytes=1024,
        max_codepoints=512,
    )
    prefix = "  --> "
    if _display_width(prefix) < width:
        available = width - _display_width(prefix)
        wrapped = _wrap_cells(location, available)
        return [
            f"  {_style('-->', 'location', color)} {wrapped[0]}",
            *(" " * len(prefix) + line for line in wrapped[1:]),
        ]
    return _wrap_cells("--> " + location, max(1, width))


def _render_span(
    span: DiagnosticSpan, *, color: bool, severity: str, width: int
) -> list[str]:
    source = _safe(span.source)
    location = source
    if span.line is not None:
        location += f":{span.line}"
        if span.column is not None:
            location += f":{span.column}"
    width = max(1, width)
    lines = _span_location_lines(location, color=color, width=width)
    gutter = _style("|", "gutter", color)
    if span.text is None:
        lines.append(f"   {gutter}" if width >= 4 else "|"[:width])
    else:
        lines.append(f"   {gutter}" if width >= 4 else "|"[:width])
        line_number = span.line or 1
        raw_text = span.text
        start = max(0, span.start or 0)
        explicit_end = span.end if span.end is not None else start + 1
        insertion = span.end is not None and explicit_end == start
        end = start if insertion else max(start + 1, explicit_end)
        expanded_text = raw_text.expandtabs(4)
        safe_text = _safe(expanded_text)
        # Span offsets address the original source.  Expand each prefix so a
        # tab before (or inside) the span moves the caret by its display width,
        # not by one source code point.
        cluster_start, cluster_end = enclosing_grapheme_span(
            raw_text, start, end
        )
        safe_before = _safe(raw_text[:cluster_start].expandtabs(4))
        safe_through = _safe(raw_text[:cluster_end].expandtabs(4))
        safe_start = _display_width(safe_before)
        safe_end = _display_width(safe_through)
        target_width = max(1, safe_end - safe_start)
        caret_length = max(
            1,
            target_width,
        )
        number = str(line_number)
        source_prefix = f" {number} | "
        caret_prefix = f" {' ' * len(number)} | "
        if width - _display_width(source_prefix) < min(2, target_width):
            source_prefix = caret_prefix = "| " if width >= 3 else ""
        source_budget = max(1, width - _display_width(source_prefix))
        # An insertion at EOF needs one display cell *after* the final source
        # grapheme. Reserve that cell before choosing the source window; if we
        # let the source consume the entire budget the otherwise-correct caret
        # would be the sole cell beyond the requested terminal width.
        insertion_at_end = insertion and cluster_start == len(raw_text)
        window_budget = max(
            1, source_budget - 1 if insertion_at_end else source_budget
        )
        window_target = (
            max(0, safe_start - 1) if insertion_at_end else safe_start
        )
        shown, actual_crop, shown_end, marker_width = _span_window(
            safe_text, window_target, window_budget
        )
        caret_start = marker_width + max(0, safe_start - actual_crop)
        caret_length = (
            1
            if insertion
            else max(
                1,
                min(
                    safe_end - safe_start,
                    max(1, shown_end - safe_start),
                    max(1, source_budget - caret_start),
                ),
            )
        )
        styled_gutter = source_prefix.replace("|", gutter, 1)
        styled_caret_gutter = caret_prefix.replace("|", gutter, 1)
        lines.append(styled_gutter + shown)

        caret = " " * caret_start + "^" * caret_length
        label = _safe(span.label) if span.label and span.label.strip() else ""
        if label and _display_width(caret + " " + label) <= source_budget:
            caret += " " + label
            label = ""
        lines.append(styled_caret_gutter + _style(caret, severity, color))
        if label:
            aligned_width = source_budget - caret_start
            label_indent = (
                min(caret_start, max(0, source_budget - 1))
                if aligned_width >= 12
                else 0
            )
            label_width = max(1, source_budget - label_indent)
            for label_line in _wrap_cells(label, label_width):
                lines.append(
                    styled_caret_gutter
                    + " " * label_indent
                    + _style(label_line, severity, color)
                )
        lines.append(f"   {gutter}" if width >= 4 else "|"[:width])
    return lines


def _metadata_lines(
    event: DiagnosticEvent, *, color: bool, width: int
) -> list[str]:
    """Render metadata as a curl-like directional wire block."""
    marker = {
        "rpc.started": ">",
        "rpc.headers": "<",
        "rpc.trailers": "<",
    }.get(event.name, "*")
    heading = {
        "rpc.started": "request metadata",
        "rpc.headers": "response headers",
        "rpc.trailers": "response trailers",
    }.get(event.name, "metadata")
    visible_metadata = [
        (key, value)
        for key, value in event.metadata
        if key.strip()
        and (bool(value) if isinstance(value, bytes) else bool(value.strip()))
    ]
    if not visible_metadata:
        return []
    marker_role = "location" if marker in {">", "<"} else "gutter"
    prefix = _style(marker, marker_role, color)
    marker_width = _display_width(marker) + 1
    content_width = max(1, width - marker_width)
    heading_lines = _wrap_cells(heading, content_width)
    lines = [
        f"{prefix} {_style(heading_line, 'strong', color)}"
        for heading_line in heading_lines
    ]
    # Keep the public metadata renderer and diagnostic sink on one wrapping
    # grammar.  Every physical continuation repeats the direction marker and
    # uses a stable two-space continuation indent inside the wire block.
    from ..rpc import render_metadata

    for content_line in render_metadata(visible_metadata, width=content_width):
        lines.append(f"{prefix} {content_line}")
    # curl terminates a header block with its bare direction marker. Keep that
    # familiar boundary while naming the gRPC metadata phase explicitly.
    if marker in {">", "<"}:
        lines.append(prefix)
    return lines


def _annotation_lines(
    label: str,
    value: str,
    *,
    role: str,
    color: bool,
    width: int,
    wrap: bool = True,
) -> list[str]:
    if not value.strip():
        return []
    plain_prefix = f"  {label}: "
    styled_prefix = f"  {_style(label, role, color)}: "
    indent_width = _display_width(plain_prefix)
    safe_value = _safe(value)
    wrapped = (
        _wrap_cells(safe_value, max(1, width - indent_width))
        if wrap
        else [safe_value]
    )
    return [
        styled_prefix + _style_semantic_text(wrapped[0], color),
        *(
            " " * indent_width + _style_semantic_text(line, color)
            for line in wrapped[1:]
        ),
    ]


def _rpc_annotation_lines(
    label: str,
    value: str,
    *,
    role: str,
    color: bool,
    width: int,
    indent: int = 2,
    value_role: str | None = None,
) -> list[str]:
    """Render a compact RPC label without the source-diagnostic gutter."""
    if not value.strip():
        return []
    padding = " " * indent
    plain_prefix = f"{padding}{label}: "
    styled_prefix = f"{padding}{_style(label, role, color)}: "
    indent_width = _display_width(plain_prefix)
    wrapped = _wrap_cells(_safe(value), max(1, width - indent_width))

    def style_value(text: str) -> str:
        if value_role is not None:
            return _style(text, value_role, color)
        return _style_semantic_text(text, color)

    return [
        styled_prefix + style_value(wrapped[0]),
        *(" " * indent_width + style_value(line) for line in wrapped[1:]),
    ]


def _headline_lines(
    event: DiagnosticEvent,
    message: str,
    *,
    color: bool,
    width: int,
    wrap: bool = True,
) -> list[str]:
    severity = event.severity.value
    label: str | None = None
    if severity == "error":
        code = (
            _abbreviate_cells(
                _safe(event.code),
                max(1, width * 8),
                max_bytes=2048,
                max_codepoints=1024,
            )
            if event.code
            else ""
        )
        label = "protodial" + (f": ({code})" if code else ":")
        plain_prefix = f"{label} "
        styled_prefix = f"{_style(label, severity, color)} "
    elif event.name == "rpc.completed":
        # A terminal non-OK status is the warning-colored counterpart to
        # ``* OK``, not a prose warning. Keep its stable W-code in machine
        # diagnostics without adding warning boilerplate to the human line.
        plain_prefix = "* "
        styled_prefix = _style("*", severity, color) + " "
    elif severity == "warning":
        code = (
            _abbreviate_cells(
                _safe(event.code),
                max(1, width * 8),
                max_bytes=2048,
                max_codepoints=1024,
            )
            if event.code
            else ""
        )
        label = "* warning" + (f" ({code})" if code else "")
        plain_prefix = f"{label}: "
        styled_prefix = f"{_style(label, severity, color)}: "
    else:
        plain_prefix = "* "
        styled_prefix = _style("*", severity, color) + " "
    indent_width = _display_width(plain_prefix)

    if not wrap:
        return [styled_prefix + _style_semantic_text(message, color)]

    # Extension codes are intentionally unrestricted. If one makes the label
    # consume most (or all) of the terminal, a conventional hanging indent
    # either exceeds ``width`` or leaves a one-cell message column followed by
    # an enormous continuation indent. Put such a label and its message in
    # separate, compact blocks. Registered E####/W#### prefixes stay on the
    # established single-line layout at every supported terminal width.
    if label is not None and indent_width > width // 2:
        label_lines = _wrap_cells(plain_prefix.rstrip(), max(1, width))
        message_indent = min(2, max(0, width - 1))
        message_lines = _wrap_cells(message, max(1, width - message_indent))
        return [
            *(_style(line, severity, color) for line in label_lines),
            *(
                " " * message_indent + _style_semantic_text(line, color)
                for line in message_lines
            ),
        ]

    wrapped = _wrap_cells(message, max(1, width - indent_width))
    return [
        styled_prefix + _style_semantic_text(wrapped[0], color),
        *(
            " " * indent_width + _style_semantic_text(line, color)
            for line in wrapped[1:]
        ),
    ]


def _rpc_failure_status(event: DiagnosticEvent, width: int) -> str:
    if "grpc_status" in event.fields or "status" in event.fields:
        name, number = _status_parts(event)
        safe_name = _abbreviate_cells(
            _safe(name),
            max(1, width * 3),
            max_bytes=1024,
            max_codepoints=512,
        )
        status_text = (
            f"{safe_name} ({number})" if number is not None else safe_name
        )
    else:
        message = _event_message(event)
        before, separator, after = message.partition(" failed with ")
        status_text = (
            after if separator and before.startswith("RPC") else message
        )

    timing = _duration_suffix(event.fields.get("elapsed_seconds"))
    counters: list[str] = []
    sent = _counter(event.fields, "sent", "sent_messages")
    if sent is not None:
        counters.append(f"sent {_count(sent)}")
    received = _counter(event.fields, "received", "received_messages")
    if received is not None:
        counters.append(f"received {_count(received)}")
    count_suffix = f"; {'; '.join(counters)}" if counters else ""
    return f"{status_text}{timing}{count_suffix}"


def _rpc_method_context(event: DiagnosticEvent, width: int) -> str | None:
    method = event.fields.get("method")
    return (
        f"`{_detail_identity(method, width)}`"
        if isinstance(method, str) and method
        else None
    )


def _rich_detail_priority(detail: DiagnosticValue) -> int:
    """Return the documented human/truncation priority of one detail."""
    if not isinstance(detail, Mapping):
        return _UNKNOWN_RICH_DETAIL_PRIORITY
    return _RICH_DETAIL_PRIORITY.get(
        _detail_name(detail), _UNKNOWN_RICH_DETAIL_PRIORITY
    )


def _detail_warning_text(
    detail: Mapping[str, DiagnosticValue], width: int
) -> str:
    """Render a decode warning without repeating titled custom identity."""
    warning = _safe(detail["warning"])
    type_name = _detail_identity(detail.get("type", "unknown"), width)
    raw_title = detail.get("title")
    if (
        not _detail_name(detail)
        and _has_visible_detail_value(raw_title)
        and _detail_identity(raw_title, width) != type_name
    ):
        return warning
    return f"`{type_name}` detail: {warning}"


def _prioritized_rich_details(
    details: Sequence[DiagnosticValue],
) -> list[tuple[int, DiagnosticValue]]:
    """Return indexed details in stable semantic presentation order."""
    buckets: list[list[tuple[int, DiagnosticValue]]] = [
        [] for _ in range(_UNKNOWN_RICH_DETAIL_PRIORITY + 1)
    ]
    for item in enumerate(details):
        buckets[_rich_detail_priority(item[1])].append(item)
    return [item for bucket in buckets for item in bucket]


def _rpc_failure_detail_group(
    detail: DiagnosticValue,
    *,
    field_prefix: str | None,
    color: bool,
    width: int,
) -> list[str]:
    """Render one detail as an indivisible truncation group."""
    detail_lines = _human_detail(
        detail,
        color=color,
        field_prefix=field_prefix,
        width=width,
    )
    if isinstance(detail, Mapping) and _has_visible_detail_value(
        detail.get("warning")
    ):
        detail_lines.extend(
            _rpc_annotation_lines(
                "note",
                _detail_warning_text(detail, width),
                role="strong",
                color=color,
                width=width,
            )
        )
    return [_style_semantic_text(line, color) for line in detail_lines]


def _rpc_failure_details(
    event: DiagnosticEvent,
    *,
    field_prefix: str | None,
    color: bool,
    width: int,
) -> list[str]:
    lines: list[str] = []
    for _, detail in _prioritized_rich_details(event.details):
        lines.extend(
            _rpc_failure_detail_group(
                detail,
                field_prefix=field_prefix,
                color=color,
                width=width,
            )
        )
    return lines


def _rpc_failure_annotations(
    label: str,
    values: Sequence[str],
    *,
    role: str,
    color: bool,
    width: int,
) -> list[str]:
    lines: list[str] = []
    for value in values:
        lines.extend(
            _rpc_annotation_lines(
                label,
                value,
                role=role,
                color=color,
                width=width,
            )
        )
    return lines


def _render_rpc_failure(
    event: DiagnosticEvent, *, color: bool, width: int
) -> str:
    """Render RPC failures in order of diagnostic value to the caller."""
    lines: list[str] = []
    invocation = _rpc_method_context(event, width)
    request_type = event.fields.get("request_type")
    field_prefix = (
        _detail_identity(request_type.rsplit(".", 1)[-1], width)
        if isinstance(request_type, str)
        else None
    )

    lines.extend(
        _rpc_failure_details(
            event,
            field_prefix=field_prefix,
            color=color,
            width=width,
        )
    )
    lines.extend(
        _headline_lines(
            event, _rpc_failure_status(event, width), color=color, width=width
        )
    )
    lines.extend(
        _rpc_failure_annotations(
            "reason",
            event.causes,
            role="strong",
            color=color,
            width=width,
        )
    )
    if invocation is not None:
        lines.extend(
            _rpc_annotation_lines(
                "call",
                invocation,
                role="strong",
                color=color,
                width=width,
                value_role="location",
            )
        )
    lines.extend(
        _rpc_failure_annotations(
            "help",
            event.help,
            role="help",
            color=color,
            width=width,
        )
    )
    lines.extend(
        _rpc_failure_annotations(
            "note",
            event.notes,
            role="strong",
            color=color,
            width=width,
        )
    )
    if event.metadata:
        lines.extend(_metadata_lines(event, color=color, width=width))
    return "\n".join(lines)


def render_human(
    event: DiagnosticEvent, *, color: bool = False, width: int = 100
) -> str:
    """Render one event using the human grammar."""
    if event.name in {"rpc.headers", "rpc.trailers"}:
        return "\n".join(_metadata_lines(event, color=color, width=width))
    if event.name == "rpc.failed":
        return _render_rpc_failure(event, color=color, width=width)
    severity = event.severity.value
    preserve_schema_lines = (
        event.name == "input.invalid"
        and event.fields.get("kind") == "protojson_schema"
    )
    lines = _headline_lines(
        event,
        _event_message(event),
        color=color,
        width=width,
        wrap=not preserve_schema_lines,
    )
    for span in event.spans:
        lines.extend(
            _render_span(span, color=color, severity=severity, width=width)
        )
    for cause in event.causes:
        lines.extend(
            _annotation_lines(
                "reason",
                cause,
                role="strong",
                color=color,
                width=width,
                wrap=not preserve_schema_lines,
            )
        )
    for help_item in event.help:
        lines.extend(
            _annotation_lines(
                "help",
                help_item,
                role="help",
                color=color,
                width=width,
            )
        )
    for note in event.notes:
        lines.extend(
            _annotation_lines(
                "note",
                note,
                role="strong",
                color=color,
                width=width,
            )
        )
    metadata = (
        _metadata_lines(event, color=color, width=width)
        if event.metadata
        else []
    )
    if metadata:
        if event.name != "rpc.started":
            lines.append("")
        lines.extend(metadata)
    for _, detail in _prioritized_rich_details(event.details):
        detail_lines = _human_detail(detail, color=color, width=width)
        if isinstance(detail, Mapping) and _has_visible_detail_value(
            detail.get("warning")
        ):
            detail_lines.extend(
                _annotation_lines(
                    "note",
                    _detail_warning_text(detail, width),
                    role="strong",
                    color=color,
                    width=width,
                )
            )
        if detail_lines:
            lines.extend(
                (
                    "",
                    *(
                        _style_semantic_text(line, color)
                        for line in detail_lines
                    ),
                )
            )
    return "\n".join(lines)


def _detail_name(detail: Mapping[str, DiagnosticValue]) -> str:
    """Return a recognized standard detail name, or an empty string."""
    return standard_detail_name(detail.get("type", "detail")) or ""


def _has_visible_detail_value(value: object) -> bool:
    """Return whether a known detail value adds visible information."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(_has_visible_detail_value(item) for item in mapping.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence = cast(Sequence[object], value)
        return any(_has_visible_detail_value(item) for item in sequence)
    return bool(value)


def _detail_value_lines(
    prefix: str,
    value: object,
    *,
    width: int,
    styled_prefix: str | None = None,
) -> list[str]:
    """Wrap a detail value with a stable, readable hanging indent."""
    safe_value = _safe(value)
    bounded_prefix = _abbreviate_cells(prefix, _detail_prefix_budget(width))
    prefix_width = _display_width(bounded_prefix)
    available = max(1, width - prefix_width)
    wrapped = _wrap_cells(safe_value, available)
    plain = [
        bounded_prefix + wrapped[0],
        *(" " * prefix_width + line for line in wrapped[1:]),
    ]
    if any(_display_width(line) > width for line in plain):
        # A value can begin with a wide grapheme that does not fit in the
        # prefix's remaining cells. Reflow the complete plain line instead.
        return _wrap_cells(bounded_prefix + safe_value, max(1, width))
    if styled_prefix is not None:
        plain[0] = (
            styled_prefix + wrapped[0]
            if bounded_prefix == prefix
            else (
                _style(bounded_prefix, "strong", True)
                if "\x1b" in styled_prefix
                else bounded_prefix
            )
            + wrapped[0]
        )
    return plain


def _render_bad_request(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str] | None:
    """Render BadRequest field violations when present."""
    if "field_violations" not in detail:
        return None
    lines: list[str] = []
    violations = detail["field_violations"]
    if isinstance(violations, Sequence) and not isinstance(
        violations, (str, bytes)
    ):
        for violation in violations:
            if not isinstance(violation, Mapping):
                continue
            raw_field = violation.get("field")
            raw_description = violation.get("description")
            field = _detail_identity(
                raw_field
                if _has_visible_detail_value(raw_field)
                else "request",
                width,
            )
            description = (
                raw_description
                if _has_visible_detail_value(raw_description)
                else "invalid"
            )
            if field_prefix and not (
                field == field_prefix or field.startswith(field_prefix + ".")
            ):
                field = f"{field_prefix}.{field}"
            prefix = f"{field}: "
            styled_prefix = f"{_style(field, 'strong', color)}: "
            lines.extend(
                _detail_value_lines(
                    prefix,
                    description,
                    width=width,
                    styled_prefix=styled_prefix,
                )
            )
    return lines


def _render_error_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render ErrorInfo reason, domain, and metadata."""
    content: list[str] = []
    reason = detail.get("reason")
    domain = detail.get("domain")
    has_reason = _has_visible_detail_value(reason)
    has_domain = _has_visible_detail_value(domain)
    if has_reason and has_domain:
        content.extend(
            _detail_value_lines(
                "  ",
                f"`{_detail_identity(reason, width)}` from "
                f"`{_detail_identity(domain, width)}`",
                width=width,
            )
        )
    elif has_reason:
        content.extend(
            _detail_value_lines(
                "  reason ",
                f"`{_detail_identity(reason, width)}`",
                width=width,
            )
        )
    elif has_domain:
        content.extend(
            _detail_value_lines(
                "  domain ",
                f"`{_detail_identity(domain, width)}`",
                width=width,
            )
        )
    metadata = detail.get("metadata")
    visible_metadata = (
        [
            (key, value)
            for key, value in metadata.items()
            if _has_visible_detail_value(key)
            and _has_visible_detail_value(value)
        ]
        if isinstance(metadata, Mapping)
        else []
    )
    if visible_metadata:
        content.append("  metadata:")
        for key, value in visible_metadata:
            safe_key = _detail_identity(key, width)
            content.extend(
                _detail_value_lines(
                    f"    {safe_key}: ",
                    _render_value(value),
                    width=width,
                )
            )
    if not content:
        return []
    return [_style("error information:", "strong", color), *content]


def _render_retry_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str] | None:
    """Render RetryInfo using a compact duration."""
    if "retry_delay" not in detail:
        return None
    delay = detail["retry_delay"]
    if not _has_visible_detail_value(delay):
        return []
    rendered_delay = _safe(delay)
    if isinstance(delay, str) and delay.endswith("s"):
        try:
            rendered_delay = format_duration(float(delay[:-1]))
        except (OverflowError, ValueError):
            pass
    return _detail_value_lines("  retry after ", rendered_delay, width=width)


def _render_violation_list(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    heading: str,
    width: int,
) -> list[str]:
    """Render precondition-like typed violations."""
    lines: list[str] = []
    violations = detail.get("violations", ())
    if isinstance(violations, Sequence):
        for violation in violations:
            if not isinstance(violation, Mapping):
                continue
            subject = violation.get("subject")
            kind = violation.get("type")
            description = violation.get("description", "")
            has_subject = _has_visible_detail_value(subject)
            has_kind = _has_visible_detail_value(kind)
            has_description = _has_visible_detail_value(description)
            if not (has_subject or has_kind or has_description):
                continue
            context = ""
            if has_kind and has_subject:
                context = (
                    f"`{_detail_identity(kind, width)}` on "
                    f"`{_detail_identity(subject, width)}`: "
                )
            elif has_subject:
                context = f"`{_detail_identity(subject, width)}`: "
            elif has_kind:
                context = f"`{_detail_identity(kind, width)}`: "
            if has_description:
                lines.extend(
                    _detail_value_lines(
                        f"  - {context}", description, width=width
                    )
                )
            else:
                lines.extend(
                    _detail_value_lines(
                        "  - ", context.removesuffix(": "), width=width
                    )
                )
    if not lines:
        return []
    return [_style(heading, "strong", color), *lines]


def _render_precondition_failure(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render PreconditionFailure violations."""
    return _render_violation_list(
        detail, color, "precondition violations:", width
    )


def _render_quota_failure(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render QuotaFailure violations."""
    return _render_violation_list(detail, color, "quota violations:", width)


def _render_resource_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render ResourceInfo identity, owner, and description."""
    lines: list[str] = []
    resource_type = detail.get("resource_type")
    resource_name = detail.get("resource_name")
    has_type = _has_visible_detail_value(resource_type)
    has_name = _has_visible_detail_value(resource_name)
    if has_type and has_name:
        lines.extend(
            _detail_value_lines(
                "  ",
                f"`{_detail_identity(resource_type, width)}` resource "
                f"`{_detail_identity(resource_name, width)}`",
                width=width,
            )
        )
    elif has_name:
        lines.extend(
            _detail_value_lines(
                "  resource ",
                f"`{_detail_identity(resource_name, width)}`",
                width=width,
            )
        )
    elif has_type:
        lines.extend(
            _detail_value_lines(
                "  resource type ",
                f"`{_detail_identity(resource_type, width)}`",
                width=width,
            )
        )
    if _has_visible_detail_value(detail.get("owner")):
        lines.extend(
            _detail_value_lines(
                "  owned by ",
                f"`{_detail_identity(detail['owner'], width)}`",
                width=width,
            )
        )
    if _has_visible_detail_value(detail.get("description")):
        lines.extend(
            _detail_value_lines("  ", detail["description"], width=width)
        )
    if not lines:
        return []
    return [_style("resource information:", "strong", color), *lines]


def _render_request_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render RequestInfo request and serving identifiers."""
    lines: list[str] = []
    request_id = detail.get("request_id")
    serving_data = detail.get("serving_data")
    has_request_id = _has_visible_detail_value(request_id)
    has_serving_data = _has_visible_detail_value(serving_data)
    if has_request_id and has_serving_data:
        lines.extend(
            _detail_value_lines(
                "  request ",
                f"`{_detail_identity(request_id, width)}` served by "
                f"`{_detail_identity(serving_data, width)}`",
                width=width,
            )
        )
    elif has_request_id:
        lines.extend(
            _detail_value_lines(
                "  request ",
                f"`{_detail_identity(request_id, width)}`",
                width=width,
            )
        )
    elif has_serving_data:
        lines.extend(
            _detail_value_lines(
                "  served by ",
                f"`{_detail_identity(serving_data, width)}`",
                width=width,
            )
        )
    if not lines:
        return []
    return [_style("request information:", "strong", color), *lines]


def _render_debug_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render DebugInfo text and stack entries."""
    lines: list[str] = []
    if _has_visible_detail_value(detail.get("detail")):
        lines.extend(_detail_value_lines("  ", detail["detail"], width=width))
    entries = detail.get("stack_entries")
    if isinstance(entries, Sequence) and not isinstance(entries, str):
        for entry in entries:
            if _has_visible_detail_value(entry):
                lines.extend(_detail_value_lines("  - ", entry, width=width))
    if not lines:
        return []
    return [_style("debug information:", "strong", color), *lines]


def _render_help(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str] | None:
    """Render Help links when present."""
    if "links" not in detail:
        return None
    lines: list[str] = []
    links = detail["links"]
    if isinstance(links, Sequence) and not isinstance(links, (str, bytes)):
        for link in links:
            if not isinstance(link, Mapping):
                continue
            description = link.get("description")
            url = link.get("url")
            has_description = _has_visible_detail_value(description)
            has_url = _has_visible_detail_value(url)
            if not (has_description or has_url):
                continue
            if has_description and has_url:
                lines.extend(
                    _detail_value_lines(
                        f"  {_detail_identity(description, width)}: ",
                        url,
                        width=width,
                    )
                )
            else:
                visible_value = description if has_description else url
                lines.extend(
                    _detail_value_lines("  ", visible_value, width=width)
                )
    if not lines:
        return []
    return [_style("help:", "strong", color), *lines]


def _render_localized_message(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render a LocalizedMessage with its locale."""
    locale = detail.get("locale")
    message = detail.get("message", "")
    if not _has_visible_detail_value(message):
        return []
    heading = "localized message"
    if _has_visible_detail_value(locale):
        heading += f" ({_detail_identity(locale, width)})"
    heading_lines = _wrap_cells(heading + ":", max(1, width))
    return [
        *(_style(line, "strong", color) for line in heading_lines),
        *_detail_value_lines("  ", message, width=width),
    ]


def _render_unknown_detail(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    width: int,
) -> list[str]:
    """Render an unknown protobuf detail without discarding its fields."""
    type_name = _abbreviate_cells(
        _safe(detail.get("type", "detail")),
        max(1, width * 4),
        max_bytes=1024,
        max_codepoints=512,
    )
    raw_title = detail.get("title")
    title = (
        _detail_identity(raw_title, width)
        if _has_visible_detail_value(raw_title)
        else type_name
    )
    title_lines = _wrap_cells(title + ":", max(1, width))
    lines = [_style(line, "strong", color) for line in title_lines]
    # A custom title must not replace the unknown protobuf identity.  Keep the
    # familiar title-less ``type.example/Message:`` shape, but put a distinct
    # identity directly beneath a custom heading as compact context.
    if title != type_name:
        lines.extend(_detail_value_lines("  type: ", type_name, width=width))
    # ``warning`` is presented once by the caller as a note. Every other
    # sibling is payload, including simultaneous ``items`` and ``value``
    # fields used by custom Any decoders. Preserve the mapping's input order
    # for stable wire-to-human comparison.
    for key, value in detail.items():
        if key in {"title", "type", "warning"}:
            continue
        safe_key = _detail_identity(key, width)
        lines.extend(
            _detail_value_lines(
                f"  {safe_key}: ",
                _render_value(value),
                width=width,
            )
        )
    return lines


_DETAIL_RENDERERS: Mapping[
    str,
    Callable[
        [Mapping[str, DiagnosticValue], bool, str | None, int],
        list[str] | None,
    ],
] = {
    "BadRequest": _render_bad_request,
    "ErrorInfo": _render_error_info,
    "RetryInfo": _render_retry_info,
    "PreconditionFailure": _render_precondition_failure,
    "QuotaFailure": _render_quota_failure,
    "ResourceInfo": _render_resource_info,
    "RequestInfo": _render_request_info,
    "DebugInfo": _render_debug_info,
    "Help": _render_help,
    "LocalizedMessage": _render_localized_message,
}


def _human_detail(
    detail: DiagnosticValue,
    *,
    color: bool,
    field_prefix: str | None = None,
    width: int = 100,
) -> list[str]:
    """Render one rich-status detail with a type-specific renderer."""
    if not isinstance(detail, Mapping):
        return _detail_value_lines("  ", _render_value(detail), width=width)
    renderer = _DETAIL_RENDERERS.get(_detail_name(detail))
    if renderer is not None:
        rendered = renderer(detail, color, field_prefix, width)
        # Known semantic details are either rendered semantically or omitted;
        # falling through would expose their empty protobuf representation.
        return rendered or []
    return _render_unknown_detail(detail, color, width)


def _rich_detail_truncation_items(
    detail: DiagnosticValue,
    *,
    field_prefix: str | None,
    color: bool,
    width: int,
) -> list[_DetailTruncationItem]:
    """Render independently selectable semantic items from one detail.

    Ordinary rendering deliberately treats a protobuf detail as one section.
    A bounded rendering needs a finer unit: an enormous first list item must
    not hide a later concise correction from the same detail.  Section
    headings remain shared presentation scaffolding and are emitted once when
    at least one of their items survives.
    """

    def styled(lines: Sequence[str]) -> list[str]:
        return [_style_semantic_text(line, color) for line in lines]

    def item_from_lines(
        lines: Sequence[str], *, heading: bool
    ) -> _DetailTruncationItem | None:
        rendered = styled(lines)
        if not rendered:
            return None
        if heading:
            if len(rendered) == 1:
                return None
            return _DetailTruncationItem(
                "\n".join(rendered[1:]), heading=rendered[0]
            )
        return _DetailTruncationItem("\n".join(rendered))

    items: list[_DetailTruncationItem] = []
    semantic_name = _detail_name(detail) if isinstance(detail, Mapping) else ""

    if isinstance(detail, Mapping) and semantic_name == "BadRequest":
        violations = detail.get("field_violations", ())
        if isinstance(violations, Sequence) and not isinstance(
            violations, (str, bytes)
        ):
            for violation in violations:
                if not isinstance(violation, Mapping):
                    continue
                rendered = _render_bad_request(
                    {**detail, "field_violations": (violation,)},
                    color,
                    field_prefix,
                    width,
                )
                candidate = item_from_lines(rendered or (), heading=False)
                if candidate is not None:
                    items.append(candidate)
    elif isinstance(detail, Mapping) and semantic_name in {
        "PreconditionFailure",
        "QuotaFailure",
    }:
        renderer = (
            _render_precondition_failure
            if semantic_name == "PreconditionFailure"
            else _render_quota_failure
        )
        violations = detail.get("violations", ())
        if isinstance(violations, Sequence):
            for violation in violations:
                if not isinstance(violation, Mapping):
                    continue
                candidate = item_from_lines(
                    renderer(
                        {**detail, "violations": (violation,)},
                        color,
                        field_prefix,
                        width,
                    ),
                    heading=True,
                )
                if candidate is not None:
                    items.append(candidate)
    elif isinstance(detail, Mapping) and semantic_name == "Help":
        links = detail.get("links", ())
        if isinstance(links, Sequence) and not isinstance(links, (str, bytes)):
            for link in links:
                if not isinstance(link, Mapping):
                    continue
                rendered = _render_help(
                    {**detail, "links": (link,)},
                    color,
                    field_prefix,
                    width,
                )
                candidate = item_from_lines(rendered or (), heading=True)
                if candidate is not None:
                    items.append(candidate)
    elif isinstance(detail, Mapping) and semantic_name == "DebugInfo":
        if _has_visible_detail_value(detail.get("detail")):
            candidate = item_from_lines(
                _render_debug_info(
                    {**detail, "stack_entries": ()},
                    color,
                    field_prefix,
                    width,
                ),
                heading=True,
            )
            if candidate is not None:
                items.append(candidate)
        entries = detail.get("stack_entries", ())
        if isinstance(entries, Sequence) and not isinstance(entries, str):
            for entry in entries:
                if not _has_visible_detail_value(entry):
                    continue
                candidate = item_from_lines(
                    _render_debug_info(
                        {**detail, "detail": "", "stack_entries": (entry,)},
                        color,
                        field_prefix,
                        width,
                    ),
                    heading=True,
                )
                if candidate is not None:
                    items.append(candidate)
    else:
        candidate = item_from_lines(
            _human_detail(
                detail,
                color=color,
                field_prefix=field_prefix,
                width=width,
            ),
            heading=False,
        )
        if candidate is not None:
            items.append(candidate)

    if isinstance(detail, Mapping) and _has_visible_detail_value(
        detail.get("warning")
    ):
        warning = styled(
            _rpc_annotation_lines(
                "note",
                _detail_warning_text(detail, width),
                role="strong",
                color=color,
                width=width,
            )
        )
        if warning:
            # A decode warning is a sibling of the semantic payload, not part
            # of a list item.  It can therefore survive independently too.
            items.append(_DetailTruncationItem("\n".join(warning)))

    return items


def _json_value(value: DiagnosticValue) -> object:
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("diagnostic JSON cannot contain non-finite numbers")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_json_value(item) for item in value]
    return value


def _span_json(span: DiagnosticSpan) -> dict[str, object]:
    value: dict[str, object] = {"source": span.source}
    for key in ("line", "column", "text", "start", "end", "label"):
        item = getattr(span, key)
        if item is not None and item != "":
            value[key] = item
    return value


def _json_object(event: DiagnosticEvent, sequence: int) -> dict[str, object]:
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
        "message": _event_message(event, sanitize=False),
    }
    if event.code:
        value["code"] = event.code
    if event.fields:
        value["context"] = _json_value(event.fields)
    if event.spans:
        value["spans"] = [_span_json(span) for span in event.spans]
    if event.causes:
        value["causes"] = list(event.causes)
    if event.help:
        value["help"] = list(event.help)
    if event.notes:
        value["notes"] = list(event.notes)
    if event.metadata:
        metadata: list[dict[str, object]] = []
        for name, raw in event.metadata:
            metadata.append({"name": name, "value": _json_value(raw)})
        value["metadata"] = metadata
    if event.details:
        value["details"] = _json_value(event.details)
    return value


def render_json(event: DiagnosticEvent, sequence: int) -> str:
    """Render one event as compact schema-v1 JSON."""
    event.validate()
    return _compact_json(_json_object(event, sequence))


def _truncate_human(
    rendered: str,
    limit: int,
    *,
    color: bool,
    subject: str = "diagnostic",
    width: int | None = None,
    _metrics: PrefixSearchMetrics | None = None,
) -> str:
    """Bound human output in UTF-8 bytes with an exact omission count."""

    def marker(omitted: int) -> str:
        message = f"{subject} truncated; {omitted} characters omitted"
        if width is not None:
            return "\n" + _human_truncation_notice(
                message,
                color=color,
                width=width,
            )
        reset = "\x1b[0m" if color else ""
        return f"{reset}\n  note: {message}"

    return truncate_with_marker(
        rendered,
        limit,
        marker=marker,
        fallback=f"{subject} truncated",
        ansi_safe=True,
        metrics=_metrics,
    )


def _human_truncation_notice(message: str, *, color: bool, width: int) -> str:
    """Render a terminal-cell-bounded truncation note with a hanging indent."""
    plain_prefix = "  note: "
    continuation = " " * _display_width(plain_prefix)
    wrapped = _wrap_cells(message, max(1, width - _display_width(plain_prefix)))
    # Avoid leaving a single trailing word on an otherwise empty continuation
    # when the preceding line can give up its final word. In particular, keep
    # the terminal phrase ``characters omitted`` together at narrow widths.
    if len(wrapped) >= 2 and len(wrapped[-1].split()) == 1:
        previous_words = wrapped[-2].split()
        if len(previous_words) > 1:
            balanced_tail = previous_words[-1] + " " + wrapped[-1]
            if _display_width(balanced_tail) <= max(
                1, width - _display_width(plain_prefix)
            ):
                wrapped[-2] = " ".join(previous_words[:-1])
                wrapped[-1] = balanced_tail
    reset = "\x1b[0m" if color else ""
    return "\n".join(
        (
            reset + plain_prefix + wrapped[0],
            *(continuation + line for line in wrapped[1:]),
        )
    )


@dataclass(frozen=True)
class _HumanTruncationGroup:
    """One independently selectable block in a bounded human diagnostic."""

    priority: int
    order: int
    text: str
    section: int | None = None
    section_prefix: str = ""
    section_heading: str = ""


@dataclass(frozen=True)
class _PreparedHumanGroup:
    """Precomputed sizes and lines for one bounded-human selection unit."""

    group: _HumanTruncationGroup
    lines: tuple[str, ...]
    line_chars: tuple[int, ...]
    line_bytes: tuple[int, ...]
    text_chars: int
    text_bytes: int
    scaffold: str
    scaffold_chars: int
    scaffold_bytes: int


def _section_scaffold(group: _HumanTruncationGroup) -> str:
    """Return presentation text paid once by the first retained section item."""
    if group.section_heading:
        return group.section_prefix + group.section_heading + "\n"
    return group.section_prefix


def _prepare_human_groups(
    groups: Sequence[_HumanTruncationGroup],
    *,
    metrics: HumanTruncationMetrics | None = None,
) -> list[_PreparedHumanGroup]:
    prepared: list[_PreparedHumanGroup] = []
    for group in groups:
        if metrics is not None:
            metrics.rendered_groups += 1
        prepared.append(
            _PreparedHumanGroup(
                group=group,
                lines=(lines := tuple(group.text.split("\n"))),
                line_chars=tuple(map(len, lines)),
                line_bytes=tuple(len(line.encode("utf-8")) for line in lines),
                text_chars=len(group.text),
                text_bytes=len(group.text.encode("utf-8")),
                scaffold=(scaffold := _section_scaffold(group)),
                scaffold_chars=len(scaffold),
                scaffold_bytes=len(scaffold.encode("utf-8")),
            )
        )
    return prepared


def _assemble_selected_human_groups(
    prepared: Sequence[_PreparedHumanGroup],
    retained: Sequence[bool],
    partial_line_counts: Sequence[int] | None = None,
    *,
    metrics: HumanTruncationMetrics | None = None,
) -> str:
    """Assemble selected groups once, preserving presentation and item order."""
    fragments: list[str] = []
    seen_sections: set[int] = set()
    for index, item in enumerate(prepared):
        if metrics is not None:
            metrics.final_group_scans += 1
        group = item.group
        line_count = (
            partial_line_counts[index] if partial_line_counts is not None else 0
        )
        if retained[index]:
            text = group.text
        elif line_count:
            text = "\n".join(item.lines[:line_count])
        else:
            continue
        scaffold = ""
        if group.section is not None and group.section not in seen_sections:
            seen_sections.add(group.section)
            scaffold = item.scaffold
        fragments.append(scaffold + text)
    return "\n".join(fragments)


def _bounded_human_text(value: str, width: int) -> str:
    """Keep a concise, grapheme-safe sample of one human diagnostic value."""
    maximum = max(16, min(1024, width * 8))
    return _abbreviate_prefix(
        value,
        maximum,
        max_bytes=min(4096, maximum * 4),
        max_codepoints=maximum,
    )


def _bounded_human_span(
    span: DiagnosticSpan,
    *,
    color: bool,
    severity: str,
    width: int,
) -> str:
    """Render a compact span while retaining its highlight, caret, and label."""
    rendered = "\n".join(
        _render_span(span, color=color, severity=severity, width=width)
    )
    if len(rendered.encode("utf-8")) <= 4096:
        return rendered
    if span.text is None:
        return rendered

    raw_text = span.text
    start = max(0, span.start or 0)
    explicit_end = span.end if span.end is not None else start + 1
    insertion = span.end is not None and explicit_end == start
    end = start if insertion else max(start + 1, explicit_end)
    cluster_start, cluster_end = enclosing_grapheme_span(raw_text, start, end)
    selected = raw_text[cluster_start:cluster_end]
    if not selected:
        selected = " "
    selected = _abbreviate_prefix(
        selected,
        max(8, min(width, 128)),
        max_bytes=512,
        max_codepoints=256,
    )
    if not selected:
        selected = "…"

    compact = replace(
        span,
        text=selected,
        start=0,
        end=0 if insertion else len(selected),
        label=_bounded_human_text(span.label, width),
    )
    return "\n".join(
        _render_span(compact, color=color, severity=severity, width=width)
    )


def truncate_generic_human(
    event: DiagnosticEvent,
    rendered: str,
    limit: int,
    *,
    color: bool,
    width: int,
) -> str:
    """Structurally bound a non-RPC human diagnostic.

    The complete rendering remains unchanged when it fits. Under pressure,
    the headline, highlighted spans, and explicit remediation are considered
    first. Causes and corrective details follow, then supporting notes,
    metadata, and finally debugging or unknown detail payloads. Selection is
    independent of display order, so oversized early context cannot hide a
    later concise action.
    """
    if len(rendered.encode("utf-8")) <= limit:
        return rendered

    severity = event.severity.value
    preserve_schema_lines = (
        event.name == "input.invalid"
        and event.fields.get("kind") == "protojson_schema"
    )
    groups: list[_HumanTruncationGroup] = []
    next_order = 0

    def add_group(
        priority: int,
        text: str,
        *,
        section: int | None = None,
        section_prefix: str = "",
        section_heading: str = "",
    ) -> None:
        nonlocal next_order
        groups.append(
            _HumanTruncationGroup(
                priority,
                next_order,
                text,
                section,
                section_prefix,
                section_heading,
            )
        )
        next_order += 1

    bounded_message = _bounded_human_text(_event_message(event), width)
    add_group(
        0,
        "\n".join(
            _headline_lines(
                event,
                bounded_message,
                color=color,
                width=width,
                wrap=not preserve_schema_lines,
            )
        ),
    )

    for span in event.spans:
        add_group(
            1,
            _bounded_human_span(
                span,
                color=color,
                severity=severity,
                width=width,
            ),
        )

    for cause in event.causes:
        lines = _annotation_lines(
            "reason",
            _bounded_human_text(cause, width),
            role="strong",
            color=color,
            width=width,
            wrap=not preserve_schema_lines,
        )
        if lines:
            add_group(2, "\n".join(lines))

    for help_item in event.help:
        lines = _annotation_lines(
            "help",
            _bounded_human_text(help_item, width),
            role="help",
            color=color,
            width=width,
        )
        if lines:
            add_group(1, "\n".join(lines))

    for note in event.notes:
        lines = _annotation_lines(
            "note",
            _bounded_human_text(note, width),
            role="strong",
            color=color,
            width=width,
        )
        if lines:
            add_group(4, "\n".join(lines))

    if event.metadata:
        metadata = _metadata_lines(event, color=color, width=width)
        if metadata:
            prefix = "" if event.name == "rpc.started" else "\n"
            add_group(5, prefix + "\n".join(metadata))

    for section, (_, detail) in enumerate(
        _prioritized_rich_details(event.details), start=1
    ):
        items = _rich_detail_truncation_items(
            detail,
            field_prefix=None,
            color=color,
            width=width,
        )
        if not items:
            continue
        detail_priority = _rich_detail_priority(detail)
        # Corrective details compete with reasons; compact contextual details
        # precede notes, while debug and opaque bulk come last.
        priority = {0: 2, 1: 3, 2: 6}.get(detail_priority, 7)
        shared_heading = next(
            (item.heading for item in items if item.heading), ""
        )
        for item in items:
            add_group(
                priority,
                item.text,
                section=section,
                section_prefix="\n",
                section_heading=shared_heading,
            )

    reset = "\x1b[0m" if color else ""
    prepared = _prepare_human_groups(groups)
    retained_flags = [False] * len(groups)
    active_sections: set[int] = set()
    # Reserve the largest possible marker once so selecting thousands of
    # independent detail groups stays linear rather than repeatedly rebuilding
    # and encoding an ever-growing candidate.
    marker_reserve = len(
        (
            f"\n{reset}  note: diagnostic truncated; "
            f"{len(rendered)} characters omitted"
        ).encode()
    )
    body_bytes = 0
    selected_count = 0
    selection_order = sorted(
        range(len(groups)),
        key=lambda index: (groups[index].priority, groups[index].order),
    )
    for index in selection_order:
        item = prepared[index]
        group = item.group
        add_scaffold = (
            item.scaffold
            if group.section is not None
            and group.section not in active_sections
            else ""
        )
        added_bytes = (
            (1 if selected_count else 0)
            + (item.scaffold_bytes if add_scaffold else 0)
            + item.text_bytes
        )
        if body_bytes + added_bytes + marker_reserve <= limit:
            retained_flags[index] = True
            selected_count += 1
            body_bytes += added_bytes
            if group.section is not None:
                active_sections.add(group.section)

    retained = _assemble_selected_human_groups(prepared, retained_flags)
    # This is the exact net character reduction from the complete human
    # rendering. Compact ellipses and scaffolding are already represented in
    # ``retained`` and therefore cannot make the count drift.
    omitted = max(1, len(rendered) - len(retained))
    marker = (
        f"{reset}  note: diagnostic truncated; {omitted} characters omitted"
    )
    bounded = "\n".join(part for part in (retained, marker) if part)
    if selected_count and len(bounded.encode("utf-8")) <= limit:
        return bounded

    # Caller-selected tiny limits may not hold even the omission notice and a
    # compact headline. Preserve grapheme and ANSI integrity in that case.
    essential = groups[0].text if groups else rendered
    return truncate_with_marker(
        essential,
        limit,
        marker=lambda essential_omitted: (
            f"{reset}\n  note: diagnostic truncated; "
            f"{len(rendered) - len(essential) + essential_omitted} "
            "characters omitted"
        ),
        fallback="diagnostic truncated",
        ansi_safe=True,
    )


def truncate_rpc_failure_human(
    event: DiagnosticEvent,
    limit: int,
    *,
    color: bool,
    width: int,
    _metrics: HumanTruncationMetrics | None = None,
) -> str:
    """Bound a failure while retaining its status, reason, and call context.

    Rich details lead the normal layout because they are often the most
    actionable content.  Under an output cap, however, they must not consume
    the status block that explains which call failed.  Reserve that compact
    block first, then use the remaining budget for whole wrapped detail lines.
    """

    def abbreviated(value: str, maximum: int = 1024) -> str:
        return _abbreviate_prefix(
            value,
            maximum,
            max_bytes=maximum * 4,
            max_codepoints=maximum,
        )

    essential_fields = dict(event.fields)
    for name in ("method", "status"):
        value = essential_fields.get(name)
        if isinstance(value, str):
            essential_fields[name] = abbreviated(value)
    grpc_status = essential_fields.get("grpc_status")
    if isinstance(grpc_status, Mapping):
        bounded_status = dict(grpc_status)
        status_name = bounded_status.get("name")
        if isinstance(status_name, str):
            bounded_status["name"] = abbreviated(status_name)
        essential_fields["grpc_status"] = bounded_status
    # Server prose and the method identifier are untrusted and may be large.
    # Keep a useful sample of each essential section so one pathological value
    # cannot displace the other sections.
    essential_event = replace(
        event,
        message=abbreviated(event.message),
        fields=essential_fields,
        causes=tuple(abbreviated(value) for value in event.causes[:4]),
        help=tuple(abbreviated(value) for value in event.help[:4]),
        notes=tuple(abbreviated(value) for value in event.notes[:4]),
        metadata=(),
        details=(),
    )
    essential = _render_rpc_failure(essential_event, color=color, width=width)
    request_type = event.fields.get("request_type")
    field_prefix = (
        _detail_identity(request_type.rsplit(".", 1)[-1], width)
        if isinstance(request_type, str)
        else None
    )
    groups: list[_HumanTruncationGroup] = []
    order = 0
    for section, (_, detail) in enumerate(
        _prioritized_rich_details(event.details), start=1
    ):
        items = _rich_detail_truncation_items(
            detail,
            field_prefix=field_prefix,
            color=color,
            width=width,
        )
        shared_heading = next(
            (item.heading for item in items if item.heading), ""
        )
        for item in items:
            groups.append(
                _HumanTruncationGroup(
                    priority=_rich_detail_priority(detail),
                    order=order,
                    text=item.text,
                    section=section,
                    section_heading=shared_heading,
                )
            )
            order += 1

    prepared = _prepare_human_groups(groups, metrics=_metrics)
    full_chars = 0
    full_sections: set[int] = set()
    for index, item in enumerate(prepared):
        group = item.group
        if index:
            full_chars += 1
        if group.section is not None and group.section not in full_sections:
            full_sections.add(group.section)
            full_chars += item.scaffold_chars
        full_chars += item.text_chars

    essential_bytes = len(essential.encode("utf-8"))
    retained = [False] * len(prepared)
    partial_line_counts = [0] * len(prepared)
    active_sections: set[int] = set()
    selected_group_count = 0
    preview_chars = 0
    preview_bytes = 0

    def marker_for(candidate_chars: int) -> str:
        omitted = full_chars - candidate_chars
        if omitted:
            message = (
                f"rich status truncated; {omitted} "
                "rich-detail characters omitted"
            )
        else:
            message = "diagnostic truncated to fit the 64 KiB sink limit"
        return _human_truncation_notice(
            message,
            color=color,
            width=width,
        )

    def fits(candidate_chars: int, candidate_bytes: int) -> bool:
        if candidate_chars > MAX_RICH_DETAIL_PREVIEW_CHARS:
            return False
        marker = marker_for(candidate_chars)
        total_bytes = (
            candidate_bytes
            + (1 if candidate_chars else 0)
            + essential_bytes
            + 1
            + len(marker.encode("utf-8"))
        )
        return total_bytes <= limit

    # Consider every semantic item as a whole before spending spare space on
    # previews. Sizes and section scaffolding are accounted incrementally, so
    # a huge first item cannot hide a later concise correction and selection
    # remains linear in the number of items.
    for index, item in enumerate(prepared):
        if _metrics is not None:
            _metrics.selection_candidates += 1
        group = item.group
        add_scaffold = (
            item.scaffold
            if group.section is not None
            and group.section not in active_sections
            else ""
        )
        added_chars = (
            (1 if selected_group_count else 0)
            + (item.scaffold_chars if add_scaffold else 0)
            + item.text_chars
        )
        added_bytes = (
            (1 if selected_group_count else 0)
            + (item.scaffold_bytes if add_scaffold else 0)
            + item.text_bytes
        )
        if fits(preview_chars + added_chars, preview_bytes + added_bytes):
            retained[index] = True
            selected_group_count += 1
            preview_chars += added_chars
            preview_bytes += added_bytes
            if group.section is not None:
                active_sections.add(group.section)

    # Use remaining space for complete physical lines from rejected items.
    # At most one non-fitting line is tried per rejected group; accepted lines
    # consume the finite preview budget, giving a deterministic linear bound.
    for index, item in enumerate(prepared):
        if retained[index]:
            continue
        group = item.group
        for line_index, _line in enumerate(item.lines):
            if _metrics is not None:
                _metrics.partial_line_candidates += 1
            first_line = line_index == 0
            add_scaffold = (
                item.scaffold
                if first_line
                and group.section is not None
                and group.section not in active_sections
                else ""
            )
            separator = 1 if first_line and selected_group_count else 0
            internal_newline = 1 if not first_line else 0
            added_chars = (
                separator
                + internal_newline
                + (item.scaffold_chars if add_scaffold else 0)
                + item.line_chars[line_index]
            )
            added_bytes = (
                separator
                + internal_newline
                + (item.scaffold_bytes if add_scaffold else 0)
                + item.line_bytes[line_index]
            )
            if not fits(
                preview_chars + added_chars, preview_bytes + added_bytes
            ):
                break
            partial_line_counts[index] += 1
            preview_chars += added_chars
            preview_bytes += added_bytes
            if first_line:
                selected_group_count += 1
                if group.section is not None:
                    active_sections.add(group.section)

    preview = _assemble_selected_human_groups(
        prepared,
        retained,
        partial_line_counts,
        metrics=_metrics,
    )
    marker = marker_for(len(preview))
    rendered = "\n".join(part for part in (preview, essential, marker) if part)
    if len(rendered.encode("utf-8")) <= limit:
        return rendered

    # Extremely small caller-selected limits cannot hold all essentials.
    # Since the essential block starts with the status, the generic bounded
    # fallback still retains the highest-priority fragment.
    subject = "rich status" if full_chars else "diagnostic"
    return _truncate_human(
        essential,
        limit,
        color=color,
        subject=subject,
        width=width,
    )


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
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
        return _abbreviate_prefix(
            value,
            max(1, string_limit),
            max_bytes=max(3, string_limit * 4),
            max_codepoints=max(1, string_limit),
        )
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            _abbreviate_cells(
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


def _minimal_json(event: DiagnosticEvent, sequence: int, limit: int) -> str:
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
                _event_message(event, sanitize=False), string_limit
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
        rendered = _compact_json(value)
        if len(rendered) <= limit:
            return rendered
        string_limit //= 2

    minimal = _compact_json(
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


def _bounded_diagnostic_json_base(
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
        raw = _json_object(replace(event, details=()), sequence)
        if len(_compact_json(raw).encode("utf-8")) <= budget:
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
                _event_message(event, sanitize=False),
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
                    _abbreviate_cells(
                        str(key), 128, max_bytes=512, max_codepoints=128
                    )
                ] = _bounded_json_value(item, string_limit, collection_limit)
            value["context"] = context
        if event.spans:
            value["spans"] = [
                _bounded_json_value(
                    _span_json(span), string_limit, collection_limit
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
            if len(_compact_json(candidate).encode("utf-8")) <= budget:
                return candidate
            string_limit //= 2

    # The validated RPC context is small enough for the loop above at the
    # production limit. Retain the established schema-valid fallback for
    # deliberately tiny private-call limits.
    return cast(
        dict[str, object],
        json.loads(_minimal_json(event, sequence, budget)),
    )


def _truncate_json_with_bounded_base(
    event: DiagnosticEvent,
    sequence: int,
    limit: int,
    *,
    _metrics: JsonTruncationMetrics | None = None,
) -> str:
    """Bound base fields, then select whole details in near-linear work."""

    details = list(event.details)
    base_budget = min(
        MAX_TRUNCATED_JSON_NON_DETAIL_CHARS,
        max(512, limit // 2),
    )
    base = _bounded_diagnostic_json_base(
        event, sequence, base_budget, _metrics=_metrics
    )

    if not details:
        rendered = _compact_json(base)
        if len(rendered.encode("utf-8")) <= limit:
            return rendered
        return _minimal_json(event, sequence, limit)

    # Normalize and serialize every detail exactly once. Compact JSON is ASCII
    # because ``ensure_ascii`` is enabled, so character and byte accounting
    # are identical from here through final assembly.
    normalized: list[object] = []
    serialized: list[str] = []
    priority_buckets: list[list[int]] = [[], [], [], []]
    for index, detail in enumerate(details):
        normalized_detail = _json_value(detail)
        normalized.append(normalized_detail)
        serialized.append(_compact_json(normalized_detail))
        priority_buckets[_rich_detail_priority(detail)].append(index)
        if _metrics is not None:
            _metrics.normalized_details += 1
            _metrics.serialized_details += 1

    retained = bytearray(len(details))
    retained_count = 0
    retained_serialized_chars = 0
    omitted_count = len(details)
    omitted_serialized_chars = (
        2 + sum(len(item) for item in serialized) + max(0, omitted_count - 1)
    )
    omitted_index_chars = (
        2
        + sum(len(str(index)) for index in range(len(details)))
        + max(0, omitted_count - 1)
    )

    compact_key_lengths = {
        str(key): len(_compact_json(str(key))) for key in base
    }
    base_notes = list(cast(Sequence[object], base.get("notes", ())))
    fixed_member_chars = 0
    fixed_member_count = 0
    for key, item in base.items():
        if key == "notes":
            continue
        fixed_member_chars += (
            compact_key_lengths[str(key)] + 1 + len(_compact_json(item))
        )
        fixed_member_count += 1

    notes_key_chars = len(_compact_json("notes")) + 1
    details_key_chars = len(_compact_json("details")) + 1
    base_note_serialized_chars = sum(
        len(_compact_json(item)) for item in base_notes
    )

    def array_chars(items_chars: int, count: int) -> int:
        return 2 + items_chars + max(0, count - 1)

    def notes_array_chars(omitted_characters: int | None) -> int | None:
        if omitted_characters is None:
            if not base_notes:
                return None
            return array_chars(base_note_serialized_chars, len(base_notes))
        note = (
            "rich status truncated; "
            f"{omitted_characters} serialized detail characters omitted"
        )
        return array_chars(
            base_note_serialized_chars + len(_compact_json(note)),
            len(base_notes) + 1,
        )

    marker_start_chars = len('{"type":"protodial.truncated","preview":')
    marker_between_preview_and_omitted = len(',"characters_omitted":')
    marker_between_omitted_and_indexes = len(',"omitted_detail_indexes":')
    marker_between_indexes_and_count = len(',"original_detail_count":')

    def marker_chars(
        preview: str,
        omitted_characters: int,
        index_array_chars: int,
    ) -> int:
        return (
            marker_start_chars
            + len(_compact_json(preview))
            + marker_between_preview_and_omitted
            + len(str(omitted_characters))
            + marker_between_omitted_and_indexes
            + index_array_chars
            + marker_between_indexes_and_count
            + len(str(len(details)))
            + 1
        )

    def object_chars(
        detail_array_chars: int,
        omitted_characters: int | None,
    ) -> int:
        note_chars = notes_array_chars(omitted_characters)
        member_chars = (
            fixed_member_chars + details_key_chars + detail_array_chars
        )
        member_count = fixed_member_count + 1
        if note_chars is not None:
            member_chars += notes_key_chars + note_chars
            member_count += 1
        return 2 + member_chars + max(0, member_count - 1)

    def truncated_candidate_chars(
        *,
        selected_chars: int,
        selected_count: int,
        remaining_chars: int,
        index_chars: int,
        preview: str = "",
    ) -> int:
        omitted_characters = remaining_chars - len(preview)
        marker_size = marker_chars(preview, omitted_characters, index_chars)
        detail_array_size = array_chars(
            selected_chars + marker_size, selected_count + 1
        )
        return object_chars(detail_array_size, omitted_characters)

    # A failed large detail does not stop the scan. Selection follows semantic
    # priority, while final assembly scans source order exactly once.
    for bucket in priority_buckets:
        for index in bucket:
            if _metrics is not None:
                _metrics.selection_candidates += 1
            trial_omitted_count = omitted_count - 1
            trial_omitted_chars = (
                omitted_serialized_chars
                - len(serialized[index])
                - (1 if omitted_count > 1 else 0)
            )
            trial_index_chars = (
                omitted_index_chars
                - len(str(index))
                - (1 if omitted_count > 1 else 0)
            )
            trial_retained_count = retained_count + 1
            trial_retained_chars = retained_serialized_chars + len(
                serialized[index]
            )
            if trial_omitted_count:
                trial_size = truncated_candidate_chars(
                    selected_chars=trial_retained_chars,
                    selected_count=trial_retained_count,
                    remaining_chars=trial_omitted_chars,
                    index_chars=trial_index_chars,
                )
            else:
                trial_size = object_chars(
                    array_chars(trial_retained_chars, trial_retained_count),
                    None,
                )
            if trial_size > limit:
                continue
            retained[index] = 1
            retained_count = trial_retained_count
            retained_serialized_chars = trial_retained_chars
            omitted_count = trial_omitted_count
            omitted_serialized_chars = trial_omitted_chars
            omitted_index_chars = trial_index_chars

    selected: list[object] = []
    omitted_indexes: list[int] = []
    omitted_serialized: list[str] = []
    for index, (value, encoded) in enumerate(
        zip(normalized, serialized, strict=True)
    ):
        if retained[index]:
            selected.append(value)
        else:
            omitted_indexes.append(index)
            omitted_serialized.append(encoded)
    if _metrics is not None:
        _metrics.final_detail_scans += len(details)

    value = dict(base)
    if not omitted_indexes:
        value["details"] = selected
        complete = _compact_json(value)
        if len(complete.encode("utf-8")) <= limit:
            return complete
        return _minimal_json(event, sequence, limit)

    remaining = "[" + ",".join(omitted_serialized) + "]"
    # Find the longest exact serialized-detail prefix that leaves room for its
    # escaped JSON string and truthful omission count. Candidate probes never
    # rebuild either the survivor list or omitted-index list.
    low = 0
    high = len(remaining)
    best_length = -1
    while low <= high:
        middle = (low + high) // 2
        preview = remaining[:middle]
        if _metrics is not None:
            _metrics.preview_candidates += 1
        trial_size = truncated_candidate_chars(
            selected_chars=retained_serialized_chars,
            selected_count=retained_count,
            remaining_chars=len(remaining),
            index_chars=omitted_index_chars,
            preview=preview,
        )
        if trial_size <= limit:
            best_length = middle
            low = middle + 1
        else:
            high = middle - 1
    if best_length < 0:
        return _minimal_json(event, sequence, limit)

    preview = remaining[:best_length]
    characters_omitted = len(remaining) - len(preview)
    selected.append(
        {
            "type": "protodial.truncated",
            "preview": preview,
            "characters_omitted": characters_omitted,
            "omitted_detail_indexes": omitted_indexes,
            "original_detail_count": len(details),
        }
    )
    notes = list(cast(Sequence[object], value.get("notes", ())))
    notes.append(
        "rich status truncated; "
        f"{characters_omitted} serialized detail characters omitted"
    )
    value["notes"] = notes
    value["details"] = selected
    rendered = _compact_json(value)
    if len(rendered.encode("utf-8")) <= limit:
        return rendered
    return _minimal_json(event, sequence, limit)


def _truncate_json(event: DiagnosticEvent, sequence: int, limit: int) -> str:
    """Return valid JSON below *limit* with bounded base and whole details."""
    return _truncate_json_with_bounded_base(event, sequence, limit)


class StderrDiagnosticSink:
    """Own formatting, sequencing, and atomic stream writes."""

    def __init__(
        self,
        verbosity: int = 1,
        *,
        stream: TextIO | None = None,
        diagnostic_format: DiagnosticFormat | str = DiagnosticFormat.HUMAN,
        color: ColorPolicy | str = ColorPolicy.AUTO,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.verbosity = max(0, verbosity)
        self.stream = stream if stream is not None else sys.stderr
        self.diagnostic_format = DiagnosticFormat(diagnostic_format)
        self.color_policy = ColorPolicy(color)
        self.environ = os.environ if environ is None else environ
        self.sequence = 0
        self._lock = threading.Lock()

    def _color_enabled(self) -> bool:
        if self.diagnostic_format is DiagnosticFormat.JSON:
            return False
        if self.color_policy is ColorPolicy.ALWAYS:
            return True
        if self.color_policy is ColorPolicy.NEVER:
            return False
        try:
            tty = self.stream.isatty()
        except (AttributeError, OSError):
            tty = False
        return (
            tty
            and self.environ.get("TERM") != "dumb"
            and "NO_COLOR" not in self.environ
        )

    def emit(self, event: DiagnosticEvent) -> None:
        """Render an eligible diagnostic and flush one atomic event write."""
        event.validate()
        if event.level > self.verbosity:
            return
        definition = DIAGNOSTIC_REGISTRY.get(event.code) if event.code else None
        aliases = DIAGNOSTIC_EVENT_ALIASES.get(event.code or "", frozenset())
        if (
            definition is not None
            and definition.event != event.name
            and event.name not in aliases
        ):
            raise ValueError(
                f"diagnostic {event.code} must use event "
                f"{definition.event!r}, not {event.name!r}"
            )
        with self._lock:
            next_sequence = self.sequence + 1
            color_enabled = self._color_enabled()
            width = _terminal_width(self.stream)
            if self.diagnostic_format is DiagnosticFormat.JSON:
                rendered = render_json(event, next_sequence)
            else:
                rendered = render_human(
                    event,
                    color=color_enabled,
                    width=width,
                )
                # Metadata phase events intentionally have no human rendering
                # when every entry is empty or whitespace-only.  Do not turn
                # that absence into a blank stderr line (or a consumed human
                # sequence number); JSON still records the phase event above.
                if not rendered.strip():
                    return
            # Leave room for the required newline while keeping the complete
            # write strictly below the documented 64 KiB guard.
            content_limit = MAX_RENDERED_DIAGNOSTIC_CHARS - 2
            if len(rendered.encode("utf-8")) > content_limit:
                if self.diagnostic_format is DiagnosticFormat.JSON:
                    rendered = _truncate_json(
                        event, next_sequence, content_limit
                    )
                elif event.name == "rpc.failed":
                    rendered = truncate_rpc_failure_human(
                        event,
                        content_limit,
                        color=color_enabled,
                        width=width,
                    )
                else:
                    rendered = truncate_generic_human(
                        event,
                        rendered,
                        content_limit,
                        color=color_enabled,
                        width=width,
                    )
            self.stream.write(rendered + "\n")
            self.stream.flush()
            self.sequence = next_sequence


def _render_value(value: DiagnosticValue) -> str:
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    metadata = _metadata_value(value)
    if metadata is not None:
        return ", ".join(
            f"{_abbreviate_cells(_safe(key), 128)}={_render_value(item)}"
            for key, item in metadata
        )
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Mapping):
        return json.dumps(
            _human_json_value(value), ensure_ascii=True, sort_keys=True
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return json.dumps(_json_value(value), ensure_ascii=True)
    return _safe(value)


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
