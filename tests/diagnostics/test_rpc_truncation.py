from __future__ import annotations

import re
from dataclasses import replace

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    HumanTruncationMetrics,
    _display_width,
    _human_truncation_notice,
    render_human,
    truncate_generic_human,
    truncate_rpc_failure_human,
)


@pytest.mark.parametrize(
    ("detail", "heading"),
    [
        (
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {"field": "bulk", "description": "x" * 70_000},
                    {"field": "resource", "description": "FIX_NOW"},
                ),
            },
            None,
        ),
        (
            {
                "type": "type.googleapis.com/google.rpc.PreconditionFailure",
                "violations": (
                    {"subject": "bulk", "description": "x" * 70_000},
                    {"subject": "resource", "description": "FIX_NOW"},
                ),
            },
            "precondition violations:",
        ),
        (
            {
                "type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": (
                    {"subject": "bulk", "description": "x" * 70_000},
                    {"subject": "resource", "description": "FIX_NOW"},
                ),
            },
            "quota violations:",
        ),
        (
            {
                "type": "type.googleapis.com/google.rpc.Help",
                "links": (
                    {"description": "bulk", "url": "x" * 70_000},
                    {"description": "resource", "url": "FIX_NOW"},
                ),
            },
            "help:",
        ),
        (
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "stack_entries": ("x" * 70_000, "resource: FIX_NOW"),
            },
            "debug information:",
        ),
    ],
)
@pytest.mark.parametrize("rpc", [False, True])
def test_semantic_list_item_truncation_keeps_later_concise_action(
    detail: dict[str, object], heading: str | None, rpc: bool
) -> None:
    event = DiagnosticEvent(
        "rpc.failed" if rpc else "extension.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        notes=("supporting context",),
        details=(detail,),
    )

    if rpc:
        rendered = truncate_rpc_failure_human(
            event, 4096, color=False, width=80
        )
    else:
        complete = render_human(event, width=80)
        rendered = truncate_generic_human(
            event, complete, 4096, color=False, width=80
        )

    assert "FIX_NOW" in rendered
    assert "truncated" in rendered
    assert len(rendered.encode("utf-8")) <= 4096
    if heading is not None:
        assert rendered.count(heading) == 1


def test_rpc_human_truncation_selection_is_linear_for_5k_groups() -> None:
    details = tuple(
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": f"field_{index:04d}", "description": "fix"},
            ),
        }
        for index in range(5_000)
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        details=details,
    )
    metrics = HumanTruncationMetrics()

    rendered = truncate_rpc_failure_human(
        event,
        64 * 1024 - 2,
        color=False,
        width=100,
        _metrics=metrics,
    )

    assert len(rendered.encode("utf-8")) <= 64 * 1024 - 2
    assert "rich status truncated" in rendered
    assert metrics.rendered_groups == 5_000
    assert metrics.selection_candidates == 5_000
    assert metrics.partial_line_candidates <= 5_000
    assert metrics.final_group_scans == 5_000


def test_rpc_semantic_item_truncation_reports_exact_net_omission() -> None:
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
                    {"field": "bulk", "description": "x" * 70_000},
                    {"field": "resource", "description": "FIX_NOW"},
                ),
            },
        ),
    )
    full = render_human(event, width=80)
    essential = render_human(replace(event, details=()), width=80)
    assert full.endswith("\n" + essential)
    full_details = full[: -(len(essential) + 1)]

    rendered = truncate_rpc_failure_human(event, 4096, color=False, width=80)
    preview, suffix = rendered.split("\n" + essential, 1)
    match = re.search(
        r"rich status truncated; (\d+) rich-detail characters omitted", suffix
    )

    assert match is not None
    assert "FIX_NOW" in preview
    assert int(match.group(1)) == len(full_details) - len(preview)


@pytest.mark.parametrize("width", [60, 61, 79, 120])
@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("omitted", [1, 12, 12_345, 12_345_678_901_234_567_890])
def test_rpc_truncation_notice_wraps_counts_with_a_hanging_indent(
    width: int, color: bool, omitted: int
) -> None:
    message = f"rich status truncated; {omitted} rich-detail characters omitted"

    rendered = _human_truncation_notice(
        message,
        color=color,
        width=width,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()
    normalized = " ".join(line.strip() for line in lines)

    assert all(_display_width(line) <= width for line in lines)
    assert all(line.startswith(" " * 8) for line in lines[1:])
    assert normalized == "note: " + message
    assert re.search(
        rf"rich status truncated; {omitted} rich-detail "
        r"characters omitted$",
        normalized,
    )
    assert rendered.count("\x1b[0m") == int(color)


@pytest.mark.parametrize("width", [60, 61, 79, 120])
@pytest.mark.parametrize("color", [False, True])
def test_rpc_generic_limit_notice_is_width_bounded_and_byte_accounted(
    width: int, color: bool
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
            "method": "examples.errors.Validator/Validate",
            "target": "localhost:50053",
        },
        metadata=(("large-context", "界" * 70_000),),
    )
    essential = render_human(
        replace(event, metadata=()), color=color, width=width
    )
    message = "diagnostic truncated to fit the 64 KiB sink limit"
    marker = _human_truncation_notice(
        message,
        color=color,
        width=width,
    )
    exact_limit = len((essential + "\n" + marker).encode("utf-8"))

    rendered = truncate_rpc_failure_human(
        event,
        exact_limit,
        color=color,
        width=width,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert rendered == essential + "\n" + marker
    assert len(rendered.encode("utf-8")) == exact_limit
    assert plain.count(message) == 1
    assert "localhost:50053" not in plain
    assert "call: `examples.errors" in plain
    assert "dator/Validate`" in plain
    assert all(_display_width(line) <= width for line in plain.splitlines())
    assert "\x1b" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered)


@pytest.mark.parametrize("width", [60, 61, 79, 120])
@pytest.mark.parametrize("color", [False, True])
def test_rpc_rich_truncation_wraps_exact_marker_with_multibyte_context(
    width: int, color: bool
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "target": "localhost:50053",
        },
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "界👩‍💻" * 12_000,
                    },
                    {
                        "field": "action",
                        "description": "FIX_NOW",
                    },
                ),
            },
        ),
    )
    full = render_human(event, color=color, width=width)
    essential = render_human(
        replace(event, details=()), color=color, width=width
    )
    assert full.endswith("\n" + essential)
    full_details = full[: -(len(essential) + 1)]

    rendered = truncate_rpc_failure_human(
        event,
        4096,
        color=color,
        width=width,
    )
    preview, suffix = rendered.split("\n" + essential, 1)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    plain_suffix = re.sub(r"\x1b\[[0-9;]*m", "", suffix)
    normalized_suffix = " ".join(
        line.strip() for line in plain_suffix.splitlines()
    )
    match = re.search(
        r"rich status truncated; (\d+) rich-detail "
        r"characters omitted$",
        normalized_suffix,
    )

    assert match is not None
    assert int(match.group(1)) == len(full_details) - len(preview)
    assert plain.count("rich status truncated") == 1
    assert "FIX_NOW" in plain
    assert "localhost:50053" not in plain
    assert "call: `examples.errors" in plain
    assert "dator/Validate`" in plain
    assert len(rendered.encode("utf-8")) <= 4096
    assert all(_display_width(line) <= width for line in plain.splitlines())
    assert "\x1b" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered)
