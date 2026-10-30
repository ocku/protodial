"""Source location, excerpt, and caret rendering."""

from __future__ import annotations

import bisect

from .._graphemes import (
    enclosing_grapheme_span,
    grapheme_clusters,
)
from ._terminal import (
    abbreviate_cells,
    cluster_width,
    display_width,
    wrap_cells,
)
from ._text import (
    safe,
    style,
)
from .models import (
    DiagnosticSpan,
)


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
    widths = [cluster_width(cluster) for cluster in clusters]
    offsets = [0]
    for cell_count in widths:
        offsets.append(offsets[-1] + cell_count)

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
    location = abbreviate_cells(
        location,
        max(1, width * 3),
        max_bytes=1024,
        max_codepoints=512,
    )
    prefix = "  --> "
    if display_width(prefix) < width:
        available = width - display_width(prefix)
        wrapped = wrap_cells(location, available)
        return [
            f"  {style('-->', 'location', color)} {wrapped[0]}",
            *(" " * len(prefix) + line for line in wrapped[1:]),
        ]
    return wrap_cells("--> " + location, max(1, width))


def render_span(
    span: DiagnosticSpan, *, color: bool, severity: str, width: int
) -> list[str]:
    source = safe(span.source)
    location = source
    if span.line is not None:
        location += f":{span.line}"
        if span.column is not None:
            location += f":{span.column}"
    width = max(1, width)
    lines = _span_location_lines(location, color=color, width=width)
    gutter = style("|", "gutter", color)
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
        safe_text = safe(expanded_text)
        # Span offsets address the original source.  Expand each prefix so a
        # tab before (or inside) the span moves the caret by its display width,
        # not by one source code point.
        cluster_start, cluster_end = enclosing_grapheme_span(
            raw_text, start, end
        )
        safe_before = safe(raw_text[:cluster_start].expandtabs(4))
        safe_through = safe(raw_text[:cluster_end].expandtabs(4))
        safe_start = display_width(safe_before)
        safe_end = display_width(safe_through)
        target_width = max(1, safe_end - safe_start)
        caret_length = max(
            1,
            target_width,
        )
        number = str(line_number)
        source_prefix = f" {number} | "
        caret_prefix = f" {' ' * len(number)} | "
        if width - display_width(source_prefix) < min(2, target_width):
            source_prefix = caret_prefix = "| " if width >= 3 else ""
        source_budget = max(1, width - display_width(source_prefix))
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
        label = safe(span.label) if span.label and span.label.strip() else ""
        if label and display_width(caret + " " + label) <= source_budget:
            caret += " " + label
            label = ""
        lines.append(styled_caret_gutter + style(caret, severity, color))
        if label:
            aligned_width = source_budget - caret_start
            label_indent = (
                min(caret_start, max(0, source_budget - 1))
                if aligned_width >= 12
                else 0
            )
            label_width = max(1, source_budget - label_indent)
            for label_line in wrap_cells(label, label_width):
                lines.append(
                    styled_caret_gutter
                    + " " * label_indent
                    + style(label_line, severity, color)
                )
        lines.append(f"   {gutter}" if width >= 4 else "|"[:width])
    return lines
