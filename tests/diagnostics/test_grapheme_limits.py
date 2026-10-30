from __future__ import annotations

import re

import pytest

from protodial.presentation import _graphemes as grapheme_module
from protodial.presentation._graphemes import (
    grapheme_boundaries,
    grapheme_clusters,
)
from protodial.presentation._truncation import (
    PrefixSearchMetrics,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    _display_width,
    _truncate_human,
    render_human,
    truncate_rpc_failure_human,
)

from .terminal_support import _CountingProperties


@pytest.mark.parametrize("color", [False, True])
def test_source_span_crop_keeps_highlight_and_cell_budgets_label(
    color: bool,
) -> None:
    glyph = "\U0001f44d\U0001f3fd"
    rendered = render_human(
        DiagnosticEvent(
            "input.invalid",
            "invalid input",
            severity=DiagnosticSeverity.ERROR,
            code="E2101",
            spans=(
                DiagnosticSpan(
                    "a/very/long/source/location.proto",
                    line=123,
                    text="x" * 80 + glyph + "y" * 80,
                    start=80,
                    end=81,
                    label="the highlighted value needs a readable explanation",
                ),
            ),
        ),
        color=color,
        width=24,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()

    assert all(_display_width(line) <= 24 for line in lines)
    assert glyph in plain
    assert "…" in plain
    assert "highlighted" in plain
    assert any("^" in line for line in lines)


def test_source_span_at_far_right_never_crops_out_selected_character() -> None:
    rendered = render_human(
        DiagnosticEvent(
            "command.failed",
            "bad",
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
            spans=(
                DiagnosticSpan(
                    "s",
                    text="a" * 100 + "X",
                    start=100,
                    end=101,
                    label="here",
                ),
            ),
        ),
        width=40,
    )
    source_line = next(
        line for line in rendered.splitlines() if "|" in line and "X" in line
    )

    assert "…" in source_line
    assert _display_width(source_line) <= 40
    assert all(_display_width(line) <= 40 for line in rendered.splitlines())


@pytest.mark.parametrize(
    "glyph",
    [
        "\U0001f1f8\U0001f1ea",
        "1\ufe0f\u20e3",
        "\u0915\u093f",
        "e\u0301",
        "\U0001f469\u200d\U0001f4bb",
        "\U0001f44d\U0001f3fd",
    ],
)
def test_human_truncation_uses_extended_grapheme_boundaries(
    glyph: str,
) -> None:
    original = "prefix " + glyph * 24 + " suffix"
    boundaries = set(grapheme_boundaries(original))
    observed_truncation = False
    for limit in range(48, min(180, len(original.encode("utf-8")))):
        truncated = _truncate_human(original, limit, color=False)
        marker = "\n  note: diagnostic truncated;"
        if marker not in truncated:
            continue
        observed_truncation = True
        prefix = truncated.split(marker, 1)[0]
        assert len(prefix) in boundaries
        assert len(truncated.encode("utf-8")) <= limit
    assert observed_truncation


def test_human_truncation_precomputes_large_prefix_index_once() -> None:
    glyph = "\U0001f469\u200d\U0001f4bb"
    original = "\x1b[31m" + glyph * 40_000 + "\x1b[0m"
    metrics = PrefixSearchMetrics()

    rendered = _truncate_human(
        original,
        4096,
        color=True,
        _metrics=metrics,
    )

    assert metrics.scanned_codepoints == len(original)
    assert metrics.safe_boundaries <= 40_003
    assert metrics.candidate_probes <= metrics.safe_boundaries.bit_length() + 1
    assert len(rendered.encode("utf-8")) <= 4096
    assert "\n  note: diagnostic truncated;" in rendered
    assert rendered.endswith("characters omitted")


def test_regional_indicator_rule_has_linear_property_reads() -> None:
    """Guard GB12/GB13 complexity without relying on wall-clock timing."""
    length = 4_001
    regional_indicator = "\U0001f1f8"
    text = regional_indicator * length
    properties = _CountingProperties(
        (grapheme_module.GCB_REGIONAL_INDICATOR,) * length
    )
    indic_properties = _CountingProperties(
        (grapheme_module.INCB_NONE,) * length
    )

    for index in range(1, length):
        grapheme_module._has_break(
            text,
            index,
            properties,
            indic_properties,
            preceding_regional_indicators_are_odd=index % 2 == 1,
        )

    assert properties.reads <= 3 * length
    assert indic_properties.reads <= length


def test_long_regional_indicator_runs_pair_from_the_start() -> None:
    regional_indicator = "\U0001f1f8"
    clusters = tuple(grapheme_clusters(regional_indicator * 1_001))

    assert clusters == (regional_indicator * 2,) * 500 + (regional_indicator,)


@pytest.mark.parametrize(
    "glyph",
    [
        "\U0001f1f8\U0001f1ea",
        "1\ufe0f\u20e3",
        "\u0915\u093f",
        "e\u0301",
        "\U0001f469\u200d\U0001f4bb",
        "\U0001f44d\U0001f3fd",
    ],
)
def test_rpc_essential_abbreviation_does_not_keep_partial_graphemes(
    glyph: str,
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        causes=("x" * 1022 + glyph + "tail",),
    )

    rendered = truncate_rpc_failure_human(
        event, 64 * 1024, color=False, width=5000
    )
    reason = next(
        line.removeprefix("  reason: ")
        for line in rendered.splitlines()
        if line.startswith("  reason: ")
    )

    assert reason == "x" * 1022 + "…"
    assert "INVALID_ARGUMENT (3)" in rendered
