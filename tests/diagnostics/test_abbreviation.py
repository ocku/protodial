from __future__ import annotations

import io
import json
import random

import pytest

from protodial.presentation._graphemes import (
    grapheme_clusters,
)
from protodial.presentation._truncation import (
    abbreviate_graphemes,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    _display_width,
    abbreviate_terminal_text,
    render_json,
)


def test_public_terminal_abbreviation_honors_tiny_byte_limits() -> None:
    assert abbreviate_terminal_text("a", 8, max_bytes=1) == "a"
    assert abbreviate_terminal_text("ab", 8, max_bytes=1) == ""
    assert abbreviate_terminal_text("abc", 8, max_bytes=2) == ""
    assert abbreviate_terminal_text("abcd", 3, max_bytes=3) == "…"
    with pytest.raises(ValueError, match="cell limit must be positive"):
        abbreviate_terminal_text("text", 0)
    with pytest.raises(ValueError, match="byte limit must be positive"):
        abbreviate_terminal_text("text", 8, max_bytes=0)


def test_public_terminal_abbreviation_fuzz_honors_exact_byte_cap() -> None:
    generator = random.Random(4103)
    atoms = ("a", "界", "e\u0301", "👍🏽", "🇸🇪")
    for _ in range(300):
        text = "".join(
            generator.choice(atoms) for _ in range(generator.randrange(40))
        )
        cells = generator.randint(1, 30)
        byte_limit = generator.randint(1, 80)
        abbreviated = abbreviate_terminal_text(
            text, cells, max_bytes=byte_limit
        )
        assert len(abbreviated.encode("utf-8")) <= byte_limit
        assert _display_width(abbreviated) <= cells
        assert "\ufffd" not in abbreviated


@pytest.mark.parametrize("kind", ["field", "code", "metadata", "span"])
def test_oversized_context_prefix_cannot_hide_short_action(kind: str) -> None:
    combining = "x" + "\u0301" * (70 * 1024)
    if kind == "field":
        event = DiagnosticEvent(
            "rpc.failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
            details=(
                {
                    "type": "type.googleapis.com/google.rpc.BadRequest",
                    "field_violations": (
                        {"field": combining, "description": "MUST_SURVIVE"},
                    ),
                },
            ),
        )
        action = "MUST_SURVIVE"
    elif kind == "code":
        event = DiagnosticEvent(
            "extension.failed",
            "IMPORTANT ACTION",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E" + "\u0301" * (70 * 1024),
        )
        action = "IMPORTANT ACTION"
    elif kind == "metadata":
        event = DiagnosticEvent(
            "rpc.headers",
            level=0,
            metadata=(("k" * 70_000, "MUST_SURVIVE"),),
        )
        action = "MUST_SURVIVE"
    else:
        event = DiagnosticEvent(
            "command.failed",
            "failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
            spans=(
                DiagnosticSpan(
                    "S" * 70_000,
                    text="bad",
                    start=0,
                    end=3,
                    label="ACTION_LABEL",
                ),
            ),
        )
        action = "ACTION_LABEL"

    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="never").emit(event)
    output = stream.getvalue()

    assert action in output
    assert len(output.encode("utf-8")) < 64 * 1024
    assert all(_display_width(line) <= 100 for line in output.splitlines())
    assert "\ufffd" not in output


@pytest.mark.parametrize("oversized_base", ["message", "metadata", "context"])
def test_generic_json_bounds_base_before_selecting_details(
    oversized_base: str,
) -> None:
    huge = "界" * (70 * 1024)
    message = huge if oversized_base == "message" else "extension failed"
    metadata = (("huge", huge),) if oversized_base == "metadata" else ()
    fields = {"huge": huge} if oversized_base == "context" else {}
    large = {
        "type": "type.googleapis.com/google.rpc.DebugInfo",
        "detail": huge,
    }
    actionable = {
        "type": "type.googleapis.com/google.rpc.BadRequest",
        "field_violations": (
            {"field": "resource", "description": "MUST_SURVIVE"},
        ),
    }
    event = DiagnosticEvent(
        "extension.failed",
        message,
        level=0,
        severity=DiagnosticSeverity.ERROR,
        fields=fields,
        metadata=metadata,
        details=(large, actionable),
    )

    # The explicit JSON API remains lossless and in raw input order.
    ordinary = json.loads(render_json(event, 1))
    assert ordinary["details"][0]["detail"] == huge

    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(event)
    output = stream.getvalue()
    value = json.loads(output)

    assert len(output.encode("utf-8")) < 64 * 1024
    assert value["details"][0]["field_violations"][0]["description"] == (
        "MUST_SURVIVE"
    )
    marker = value["details"][-1]
    omitted = json.dumps([large], ensure_ascii=True, separators=(",", ":"))
    assert marker["type"] == "protodial.truncated"
    assert marker["omitted_detail_indexes"] == [0]
    assert marker["original_detail_count"] == 2
    assert marker["preview"] == omitted[: len(marker["preview"])]
    assert marker["characters_omitted"] == len(omitted) - len(marker["preview"])


def test_dual_bound_abbreviation_handles_one_enormous_grapheme() -> None:
    text = "left-" + "x" + "\u0301" * 70_000 + "-right"

    abbreviated = abbreviate_graphemes(
        text,
        max_cells=20,
        max_bytes=40,
        max_codepoints=20,
        cluster_width=_display_width,
    )

    assert abbreviated.startswith("left-")
    assert abbreviated.endswith("-right")
    assert "…" in abbreviated
    assert _display_width(abbreviated) <= 20
    assert len(abbreviated.encode("utf-8")) <= 40
    assert len(abbreviated) <= 20
    assert tuple(grapheme_clusters(abbreviated)) == (
        "l",
        "e",
        "f",
        "t",
        "-",
        "…",
        "-",
        "r",
        "i",
        "g",
        "h",
        "t",
    )


def test_dual_bound_abbreviation_fuzz_respects_every_boundary() -> None:
    generator = random.Random(20261024)
    atoms = ("a", "界", "e\u0301", "👍🏽", "🇸🇪", "कि", "👩\u200d💻")

    for _ in range(500):
        text = "".join(
            generator.choice(atoms) for _ in range(generator.randrange(80))
        )
        max_cells = generator.randint(1, 40)
        max_bytes = generator.randint(3, 120)
        max_codepoints = generator.randint(1, 50)
        abbreviated = abbreviate_graphemes(
            text,
            max_cells=max_cells,
            max_bytes=max_bytes,
            max_codepoints=max_codepoints,
            cluster_width=_display_width,
        )

        assert _display_width(abbreviated) <= max_cells
        assert len(abbreviated.encode("utf-8")) <= max_bytes
        assert len(abbreviated) <= max_codepoints
        assert "\ufffd" not in abbreviated
        original_clusters = set(grapheme_clusters(text))
        assert all(
            cluster == "…" or cluster in original_clusters
            for cluster in grapheme_clusters(abbreviated)
        )
