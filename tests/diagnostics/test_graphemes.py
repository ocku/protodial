from __future__ import annotations

import pytest

from protodial.presentation._graphemes import (
    grapheme_clusters,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    _display_width,
    _wrap_cells,
    render_human,
)
from protodial.presentation.sanitization import escape_control_characters


def test_unicode_wrapping_and_source_carets_use_terminal_cells() -> None:
    wrapped = render_human(
        DiagnosticEvent(
            "command.failed",
            "界" * 30 + " e\u0301" * 12,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
        ),
        width=40,
    )
    assert max(_display_width(line) for line in wrapped.splitlines()) <= 40

    rendered = render_human(
        DiagnosticEvent(
            "command.failed",
            "invalid source",
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
            spans=(
                DiagnosticSpan(
                    "input",
                    line=1,
                    text="界e\u0301x",
                    start=3,
                    end=4,
                    label="here",
                ),
            ),
        )
    )
    caret = next(line for line in rendered.splitlines() if "^" in line)
    after_gutter = caret.split("| ", 1)[1]
    assert after_gutter == "   ^ here"


def test_emoji_skin_tones_stay_with_their_base_when_wrapped() -> None:
    glyph = "\U0001f44d\U0001f3fd"
    wrapped = _wrap_cells(glyph * 10, 6)

    assert _display_width(glyph) == 2
    assert "".join(wrapped) == glyph * 10
    assert all(_display_width(line) <= 6 for line in wrapped)
    assert all(not line.startswith("\U0001f3fd") for line in wrapped)


@pytest.mark.parametrize(
    "glyph",
    [
        "\U0001f1f8\U0001f1ea",  # regional-indicator flag
        "1\ufe0f\u20e3",  # keycap
        "\u0915\u093f",  # category-M spacing mark
        "e\u0301",  # combining accent
        "\U0001f469\u200d\U0001f4bb",  # ZWJ sequence
        "\U0001f44d\U0001f3fd",  # emoji modifier
    ],
)
def test_terminal_operations_keep_extended_graphemes_together(
    glyph: str,
) -> None:
    glyph_width = _display_width(glyph)
    assert glyph_width in {1, 2}
    assert _wrap_cells(glyph * 4, glyph_width) == [glyph] * 4

    rendered = render_human(
        DiagnosticEvent(
            "command.failed",
            "invalid source",
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
            spans=(
                DiagnosticSpan(
                    "input",
                    line=1,
                    text=f"x{glyph}y",
                    start=1,
                    end=2,
                    label="cluster",
                ),
            ),
        )
    )
    caret = next(line for line in rendered.splitlines() if "^" in line)
    after_gutter = caret.split("| ", 1)[1]
    rendered_glyph_width = _display_width(escape_control_characters(glyph))
    assert after_gutter == f" {'^' * rendered_glyph_width} cluster"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\r\n", ("\r\n",)),  # GB3
        ("\n\u0300", ("\n", "\u0300")),  # GB4/GB5 before GB9
        ("A\u200dB", ("A\u200d", "B")),  # ZWJ alone is not GB11
        (
            "\U0001f469\u0300\u200d\U0001f4bb",
            ("\U0001f469\u0300\u200d\U0001f4bb",),
        ),
        ("\U0001f1f8\U0001f1ea\U0001f1fa", ("🇸🇪", "🇺")),
        ("\u0e01\u0e33", ("\u0e01\u0e33",)),  # Thai SpacingMark
        ("\u0e81\u0eb3", ("\u0e81\u0eb3",)),  # Lao SpacingMark
        ("a\u200c", ("a\u200c",)),  # ZWNJ Extend
        ("\uff76\uff9e", ("\uff76\uff9e",)),  # halfwidth voiced mark
        ("\u0d4e\u0d15", ("\u0d4e\u0d15",)),  # complete Prepend data
        ("\U00013430A", ("\U00013430", "A")),  # Control, not Prepend
        ("\U00016d63\U00016d67", ("\U00016d63\U00016d67",)),
        ("\u0915\u094d\u0300\u0915", ("\u0915\u094d\u0300\u0915",)),
        ("\u0915\u094dA", ("\u0915\u094d", "A")),
    ],
)
def test_unicode_16_extended_grapheme_rule_edges(
    text: str, expected: tuple[str, ...]
) -> None:
    assert tuple(grapheme_clusters(text)) == expected
