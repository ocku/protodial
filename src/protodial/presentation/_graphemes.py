"""Unicode extended-grapheme segmentation for terminal-safe text cuts.

Python's standard library does not expose UAX #29 segmentation. This module
implements the Unicode 16.0 extended grapheme rules GB3--GB999 using compact,
generated property tables. It intentionally owns only character boundaries;
terminal wrapping and cell-width policy remain with the renderer.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator, Sequence

from ._grapheme_data import (
    EXTENDED_PICTOGRAPHIC_RANGES,
    GCB_CONTROL,
    GCB_CR,
    GCB_EXTEND,
    GCB_L,
    GCB_LF,
    GCB_LV,
    GCB_LVT,
    GCB_OTHER,
    GCB_PREPEND,
    GCB_REGIONAL_INDICATOR,
    GCB_SPACING_MARK,
    GCB_T,
    GCB_V,
    GCB_ZWJ,
    GRAPHEME_BREAK_RANGES,
    INCB_CONSONANT,
    INCB_EXTEND,
    INCB_LINKER,
    INCB_NONE,
    INDIC_CONJUNCT_BREAK_RANGES,
)

_GRAPHEME_BREAK_STARTS = tuple(item[0] for item in GRAPHEME_BREAK_RANGES)
_EXTENDED_PICTOGRAPHIC_STARTS = tuple(
    item[0] for item in EXTENDED_PICTOGRAPHIC_RANGES
)
_INDIC_CONJUNCT_BREAK_STARTS = tuple(
    item[0] for item in INDIC_CONJUNCT_BREAK_RANGES
)

_CONTROL = frozenset((GCB_CONTROL, GCB_CR, GCB_LF))
_INDIC_EXTEND_OR_LINKER = frozenset((INCB_EXTEND, INCB_LINKER))


def _range_value(
    code_point: int,
    ranges: Sequence[tuple[int, int, int]],
    starts: Sequence[int],
    default: int,
) -> int:
    index = bisect.bisect_right(starts, code_point) - 1
    if index >= 0:
        start, end, value = ranges[index]
        if start <= code_point <= end:
            return value
    return default


def _grapheme_break(character: str) -> int:
    return _range_value(
        ord(character),
        GRAPHEME_BREAK_RANGES,
        _GRAPHEME_BREAK_STARTS,
        GCB_OTHER,
    )


def _indic_conjunct_break(character: str) -> int:
    return _range_value(
        ord(character),
        INDIC_CONJUNCT_BREAK_RANGES,
        _INDIC_CONJUNCT_BREAK_STARTS,
        INCB_NONE,
    )


def _is_extended_pictographic(character: str) -> bool:
    code_point = ord(character)
    index = bisect.bisect_right(_EXTENDED_PICTOGRAPHIC_STARTS, code_point) - 1
    return (
        index >= 0
        and EXTENDED_PICTOGRAPHIC_RANGES[index][0]
        <= code_point
        <= EXTENDED_PICTOGRAPHIC_RANGES[index][1]
    )


def _joins_indic_conjunct(index: int, indic_properties: Sequence[int]) -> bool:
    """Implement GB9c immediately before the character at *index*."""
    if indic_properties[index] != INCB_CONSONANT:
        return False
    cursor = index - 1
    saw_linker = False
    while cursor >= 0 and indic_properties[cursor] in _INDIC_EXTEND_OR_LINKER:
        saw_linker |= indic_properties[cursor] == INCB_LINKER
        cursor -= 1
    return (
        saw_linker
        and cursor >= 0
        and indic_properties[cursor] == INCB_CONSONANT
    )


def _joins_emoji_zwj(text: str, index: int, properties: Sequence[int]) -> bool:
    """Implement GB11 immediately before the character at *index*."""
    if not _is_extended_pictographic(text[index]):
        return False
    cursor = index - 1
    if cursor < 0 or properties[cursor] != GCB_ZWJ:
        return False
    cursor -= 1
    while cursor >= 0 and properties[cursor] == GCB_EXTEND:
        cursor -= 1
    return cursor >= 0 and _is_extended_pictographic(text[cursor])


def _has_break(
    text: str,
    index: int,
    properties: Sequence[int],
    indic_properties: Sequence[int],
    *,
    preceding_regional_indicators_are_odd: bool,
) -> bool:
    """Apply UAX #29 rules in their normative precedence order."""
    previous = properties[index - 1]
    current = properties[index]

    # GB3, then GB4/GB5. Control precedence is significant: an Extend or
    # SpacingMark after a control still starts a new grapheme.
    if previous == GCB_CR and current == GCB_LF:
        return False
    if previous in _CONTROL or current in _CONTROL:
        return True

    # GB6--GB8: conjoining Hangul syllable sequences.
    if previous == GCB_L and current in (GCB_L, GCB_V, GCB_LV, GCB_LVT):
        return False
    if previous in (GCB_LV, GCB_V) and current in (GCB_V, GCB_T):
        return False
    if previous in (GCB_LVT, GCB_T) and current == GCB_T:
        return False

    # GB9, GB9a, and GB9b.
    if current in (GCB_EXTEND, GCB_ZWJ, GCB_SPACING_MARK):
        return False
    if previous == GCB_PREPEND:
        return False

    if _joins_indic_conjunct(index, indic_properties):  # GB9c
        return False
    if _joins_emoji_zwj(text, index, properties):  # GB11
        return False
    if (
        current == GCB_REGIONAL_INDICATOR
        and preceding_regional_indicators_are_odd
    ):  # GB12/GB13
        return False
    return True  # GB999


def grapheme_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield Unicode 16 extended-grapheme spans for *text*."""
    if not text:
        return

    properties = tuple(_grapheme_break(character) for character in text)
    indic_properties = tuple(
        _indic_conjunct_break(character) for character in text
    )
    start = 0
    # GB12/GB13 depend on the parity of the immediately preceding RI run.
    # Carrying that parity through the forward pass avoids rescanning a long
    # flag sequence at every boundary.  The bounded backward scans for GB9c
    # and GB11 are amortized linear: each scan is gated by a terminating
    # consonant or ``ZWJ + Extended_Pictographic`` pair, respectively, so an
    # Extend/Linker run cannot be traversed again by a later candidate.
    regional_indicator_run = int(properties[0] == GCB_REGIONAL_INDICATOR)
    for index in range(1, len(text)):
        if _has_break(
            text,
            index,
            properties,
            indic_properties,
            preceding_regional_indicators_are_odd=(
                regional_indicator_run % 2 == 1
            ),
        ):
            yield start, index
            start = index
        if properties[index] == GCB_REGIONAL_INDICATOR:
            regional_indicator_run = (
                regional_indicator_run + 1
                if properties[index - 1] == GCB_REGIONAL_INDICATOR
                else 1
            )
        else:
            regional_indicator_run = 0
    yield start, len(text)


def grapheme_clusters(text: str) -> Iterator[str]:
    """Yield Unicode extended grapheme clusters from *text*."""
    for start, end in grapheme_spans(text):
        yield text[start:end]


def grapheme_boundaries(text: str) -> tuple[int, ...]:
    """Return every safe code-point cut, including zero and ``len(text)``."""
    return (0, *(end for _, end in grapheme_spans(text)))


def grapheme_prefix(text: str, end: int) -> str:
    """Return the longest prefix ending no later than *end* at a safe cut."""
    bounded_end = max(0, min(end, len(text)))
    boundaries = grapheme_boundaries(text)
    boundary = boundaries[bisect.bisect_right(boundaries, bounded_end) - 1]
    return text[:boundary]


def enclosing_grapheme_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand a code-point span outward to complete grapheme boundaries."""
    boundaries = grapheme_boundaries(text)
    bounded_start = max(0, min(start, len(text)))
    bounded_end = max(bounded_start, min(end, len(text)))
    start_index = bisect.bisect_right(boundaries, bounded_start) - 1
    end_index = bisect.bisect_left(boundaries, bounded_end)
    return boundaries[start_index], boundaries[end_index]
