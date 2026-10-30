"""Grapheme-safe terminal cell measurement and wrapping."""

from __future__ import annotations

import os
import unicodedata
from typing import TextIO

from .._graphemes import (
    grapheme_clusters,
)
from .._truncation import (
    abbreviate_graphemes,
)
from ._text import (
    safe,
)


def terminal_width(stream: TextIO) -> int:
    try:
        width = os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        width = 100
    return min(120, max(60, width))


def cluster_width(cluster: str) -> int:
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


def display_width(text: str) -> int:
    """Return terminal-cell width for unstyled, sanitized text."""
    return sum(cluster_width(cluster) for cluster in grapheme_clusters(text))


def abbreviate_cells(
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
        cluster_width=cluster_width,
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
        cluster_width=cluster_width,
    )


def abbreviate_prefix(
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
        cluster_width=cluster_width,
        preserve_suffix=False,
    )


def detail_prefix_budget(width: int) -> int:
    """Reserve at least half a normal detail line for its useful value."""
    return max(1, min(48, width // 2))


def detail_identity(value: object, width: int) -> str:
    """Sanitize and independently bound contextual rich-detail text."""
    return abbreviate_cells(safe(value), detail_prefix_budget(width))


def wrap_cells(text: str, width: int) -> list[str]:
    """Wrap text by terminal cells while retaining combining sequences."""
    width = max(1, width)
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        word_width = display_width(word)
        if current and display_width(current) + 1 + word_width <= width:
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
            cell_count = cluster_width(cluster)
            if chunk and chunk_width + cell_count > width:
                lines.append(chunk)
                chunk = ""
                chunk_width = 0
            # A glyph can be wider than a deliberately tiny width. Keeping
            # its grapheme intact is preferable to corrupting it.
            chunk += cluster
            chunk_width += cell_count
        current = chunk
    if current:
        lines.append(current)
    return lines or [""]


def terminal_cell_width(text: str) -> int:
    """Return the terminal width used by public presentation renderers."""
    return display_width(text)


def wrap_terminal_cells(text: str, width: int) -> list[str]:
    """Wrap sanitized text using the shared terminal-cell policy."""
    return wrap_cells(text, width)
