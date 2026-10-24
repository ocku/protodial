"""Linear-time primitives for bounded terminal text."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ._graphemes import grapheme_boundaries, grapheme_clusters

_ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class PrefixSearchMetrics:
    """Deterministic work counters for truncation complexity tests."""

    scanned_codepoints: int = 0
    safe_boundaries: int = 0
    candidate_probes: int = 0


def abbreviate_graphemes(
    text: str,
    *,
    max_cells: int,
    max_bytes: int,
    max_codepoints: int,
    cluster_width: Callable[[str], int],
    marker: str = "…",
    preserve_suffix: bool = True,
) -> str:
    """Abbreviate plain text within three independent bounds.

    Cuts occur only between extended grapheme clusters.  Bounding both
    terminal cells and storage size matters because a single displayed glyph
    may contain an attacker-controlled number of zero-width combining marks.
    Callers sanitize terminal controls before using this plain-text primitive;
    ``preserve_suffix`` selects middle or end abbreviation. Styled output is
    bounded separately by :func:`truncate_with_marker`, which also avoids cuts
    inside ANSI SGR sequences.
    """

    if min(max_cells, max_bytes, max_codepoints) < 1:
        raise ValueError("abbreviation limits must be positive")

    clusters = tuple(grapheme_clusters(text))

    def measure(cluster: str) -> tuple[int, int, int]:
        return (
            cluster_width(cluster),
            len(cluster.encode("utf-8")),
            len(cluster),
        )

    metrics = tuple(measure(cluster) for cluster in clusters)
    totals = tuple(sum(item[index] for item in metrics) for index in range(3))
    limits = (max_cells, max_bytes, max_codepoints)
    if all(total <= limit for total, limit in zip(totals, limits, strict=True)):
        return text

    marker_metrics = measure(marker)
    if any(
        amount > limit
        for amount, limit in zip(marker_metrics, limits, strict=True)
    ):
        return ""

    used = list(marker_metrics)
    left: list[str] = []
    right: list[str] = []
    left_index = 0
    right_index = len(clusters) - 1
    left_blocked = False
    right_blocked = not preserve_suffix
    choose_left = True

    def fits(candidate: tuple[int, int, int]) -> bool:
        return all(
            current + amount <= limit
            for current, amount, limit in zip(
                used, candidate, limits, strict=True
            )
        )

    while left_index <= right_index and not (left_blocked and right_blocked):
        if choose_left and not left_blocked:
            candidate = metrics[left_index]
            if fits(candidate):
                left.append(clusters[left_index])
                left_index += 1
                used[:] = [
                    current + amount
                    for current, amount in zip(used, candidate, strict=True)
                ]
            else:
                left_blocked = True
        elif not choose_left and not right_blocked:
            candidate = metrics[right_index]
            if fits(candidate):
                right.append(clusters[right_index])
                right_index -= 1
                used[:] = [
                    current + amount
                    for current, amount in zip(used, candidate, strict=True)
                ]
            else:
                right_blocked = True
        choose_left = not choose_left if preserve_suffix else True

    right.reverse()
    return "".join((*left, marker, *right))


def _safe_prefix_index(
    text: str,
    *,
    ansi_safe: bool,
    metrics: PrefixSearchMetrics | None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Index grapheme-safe prefix cuts and their UTF-8 byte sizes once."""
    boundaries = grapheme_boundaries(text)
    byte_sizes = [0]
    for character in text:
        byte_sizes.append(byte_sizes[-1] + len(character.encode("utf-8")))

    if metrics is not None:
        metrics.scanned_codepoints += len(text)

    if ansi_safe:
        ansi_spans = tuple(
            (match.start(), match.end())
            for match in _ANSI_SEQUENCE.finditer(text)
        )
        safe: list[int] = []
        span_index = 0
        for boundary in boundaries:
            while (
                span_index < len(ansi_spans)
                and ansi_spans[span_index][1] <= boundary
            ):
                span_index += 1
            bisects_ansi = (
                span_index < len(ansi_spans)
                and ansi_spans[span_index][0]
                < boundary
                < ansi_spans[span_index][1]
            )
            follows_bare_escape = boundary > 0 and text[boundary - 1] == "\x1b"
            if not bisects_ansi and not follows_bare_escape:
                safe.append(boundary)
        boundaries = tuple(safe)

    if metrics is not None:
        metrics.safe_boundaries += len(boundaries)
    return boundaries, tuple(byte_sizes[boundary] for boundary in boundaries)


def truncate_with_marker(
    text: str,
    limit: int,
    *,
    marker: Callable[[int], str],
    fallback: str,
    ansi_safe: bool = False,
    metrics: PrefixSearchMetrics | None = None,
) -> str:
    """Return the longest safe prefix plus a marker within *limit* bytes.

    Grapheme boundaries, ANSI exclusions, and UTF-8 prefix byte sizes are
    computed once. Candidate selection then needs only logarithmically many
    constant-time prefix-size probes; the only per-probe encoding is the
    short omission marker.
    """
    boundaries, byte_sizes = _safe_prefix_index(
        text, ansi_safe=ansi_safe, metrics=metrics
    )
    best_index: int | None = None
    low = 0
    high = len(boundaries) - 1
    while low <= high:
        middle = (low + high) // 2
        boundary = boundaries[middle]
        omitted = len(text) - boundary
        marker_text = marker(omitted)
        if metrics is not None:
            metrics.candidate_probes += 1
        if byte_sizes[middle] + len(marker_text.encode("utf-8")) <= limit:
            best_index = middle
            low = middle + 1
        else:
            high = middle - 1

    if best_index is not None:
        boundary = boundaries[best_index]
        return text[:boundary] + marker(len(text) - boundary)

    return fallback.encode("utf-8")[:limit].decode("utf-8", "ignore")
