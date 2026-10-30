"""Shared bounded human text and section selection primitives."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .._graphemes import (
    enclosing_grapheme_span,
)
from .._truncation import (
    PrefixSearchMetrics,
    truncate_with_marker,
)
from ._render_policy import (
    HumanTruncationMetrics,
)
from ._spans import (
    render_span,
)
from ._terminal import (
    abbreviate_prefix,
    display_width,
    wrap_cells,
)
from .models import (
    DiagnosticSpan,
)


def truncate_human(
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
            return "\n" + human_truncation_notice(
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


def human_truncation_notice(message: str, *, color: bool, width: int) -> str:
    """Render a terminal-cell-bounded truncation note with a hanging indent."""
    plain_prefix = "  note: "
    continuation = " " * display_width(plain_prefix)
    wrapped = wrap_cells(message, max(1, width - display_width(plain_prefix)))
    # Avoid leaving a single trailing word on an otherwise empty continuation
    # when the preceding line can give up its final word. In particular, keep
    # the terminal phrase ``characters omitted`` together at narrow widths.
    if len(wrapped) >= 2 and len(wrapped[-1].split()) == 1:
        previous_words = wrapped[-2].split()
        if len(previous_words) > 1:
            balanced_tail = previous_words[-1] + " " + wrapped[-1]
            if display_width(balanced_tail) <= max(
                1, width - display_width(plain_prefix)
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
class HumanTruncationGroup:
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

    group: HumanTruncationGroup
    lines: tuple[str, ...]
    line_chars: tuple[int, ...]
    line_bytes: tuple[int, ...]
    text_chars: int
    text_bytes: int
    scaffold: str
    scaffold_chars: int
    scaffold_bytes: int


def _section_scaffold(group: HumanTruncationGroup) -> str:
    """Return presentation text paid once by the first retained section item."""
    if group.section_heading:
        return group.section_prefix + group.section_heading + "\n"
    return group.section_prefix


def prepare_human_groups(
    groups: Sequence[HumanTruncationGroup],
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


def assemble_selected_human_groups(
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


def bounded_human_text(value: str, width: int) -> str:
    """Keep a concise, grapheme-safe sample of one human diagnostic value."""
    maximum = max(16, min(1024, width * 8))
    return abbreviate_prefix(
        value,
        maximum,
        max_bytes=min(4096, maximum * 4),
        max_codepoints=maximum,
    )


def bounded_human_span(
    span: DiagnosticSpan,
    *,
    color: bool,
    severity: str,
    width: int,
) -> str:
    """Render a compact span while retaining its highlight, caret, and label."""
    rendered = "\n".join(
        render_span(span, color=color, severity=severity, width=width)
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
    selected = abbreviate_prefix(
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
        label=bounded_human_text(span.label, width),
    )
    return "\n".join(
        render_span(compact, color=color, severity=severity, width=width)
    )
