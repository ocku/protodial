from __future__ import annotations

import re

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
)
from protodial.presentation.diagnostics.rendering import (
    _display_width,
    _human_detail,
    render_human,
    truncate_rpc_failure_human,
)


def test_bad_request_request_type_prefix_is_terminal_safe() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "request_type": "example.Forged\nPrefix\x1b[2J",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {"field": "name", "description": "is invalid"},
                ),
            },
        ),
    )

    rendered = render_human(event)

    assert "Forged\\nPrefix\\x1b[2J.name: is invalid" in rendered
    assert "\x1b" not in rendered
    assert len(rendered.splitlines()) == 2


def test_rich_detail_sections_wrap_with_readable_hanging_indents() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "a readable validation explanation " * 3,
                    },
                ),
            },
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "metadata": {"trace": "metadata value " * 6},
            },
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "stack_entries": ("package.module.function(file.py:123) " * 3,),
            },
            {"type": "custom", "value": {"message": "unknown value " * 6}},
        ),
    )

    rendered = render_human(event, color=True, width=40)
    plain_lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", line) for line in rendered.splitlines()
    ]

    assert max(map(len, plain_lines)) <= 40
    assert any(line.startswith(" " * len("resource: ")) for line in plain_lines)
    assert any(
        line.startswith(" " * len("    trace: ")) for line in plain_lines
    )
    assert any(
        line.startswith("    ") and "file.py" in line for line in plain_lines
    )
    assert "custom:" in plain_lines


def test_long_rich_detail_prefixes_respect_terminal_cell_width() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "界" * 35,
                        "description": "field value is invalid",
                    },
                ),
            },
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "metadata": {"界" * 35: "metadata value"},
            },
        ),
    )

    rendered = render_human(event, color=True, width=40)
    plain_lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", line) for line in rendered.splitlines()
    ]

    assert max(_display_width(line) for line in plain_lines) <= 40


def test_oversized_rich_detail_prefixes_leave_short_values_visible() -> None:
    multibyte_prefix = ("界e\u0301" * (24 * 1024)) + "tail"
    event = DiagnosticEvent(
        "rpc.failed",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "request_type": f"example.{multibyte_prefix}",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "must not be empty",
                    },
                    {
                        "field": f"{multibyte_prefix}.resource",
                        "description": "short field value",
                    },
                ),
            },
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "metadata": {multibyte_prefix: "short metadata value"},
            },
            {
                "type": "type.googleapis.com/google.rpc.PreconditionFailure",
                "violations": (
                    {
                        "type": multibyte_prefix,
                        "subject": multibyte_prefix,
                        "description": "short precondition value",
                    },
                ),
            },
            {
                "type": "type.googleapis.com/google.rpc.Help",
                "links": (
                    {
                        "description": multibyte_prefix,
                        "url": "https://example.test/fix",
                    },
                ),
            },
        ),
    )

    rendered = truncate_rpc_failure_human(
        event, 64 * 1024, color=True, width=80
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert "resource" in plain
    assert "must not be empty" in plain
    assert "short field value" in plain
    assert "short metadata value" in plain
    assert "short precondition value" in plain
    assert "https://example.test/fix" in plain
    assert "…" in plain
    assert "\ufffd" not in plain
    assert all(_display_width(line) <= 80 for line in plain.splitlines())


def test_scalar_rich_details_wrap_to_narrow_terminal_cell_width() -> None:
    text_lines = _human_detail("界\n" * 12, color=False, width=7)
    bytes_lines = _human_detail(b"\xff" * 12, color=False, width=7)

    assert all(
        _display_width(line) <= 7 for line in (*text_lines, *bytes_lines)
    )
    assert "\\n" in "".join(line.strip() for line in text_lines)
    assert "base64:" in "".join(line.strip() for line in bytes_lines)
