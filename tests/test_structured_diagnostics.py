from __future__ import annotations

import io
import json
import os
import random
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import overload

import pytest
from google.protobuf import descriptor_pb2
from grpc_reflection.v1alpha import reflection_pb2

from protodial import cli
from protodial.argument_parser import parse_arguments
from protodial.errors import CliError
from protodial.message_io import iter_json_values
from protodial.message_io.models import (
    JsonMessageSchemaError,
    MessageParseError,
)
from protodial.presentation import _graphemes as grapheme_module
from protodial.presentation._graphemes import (
    grapheme_boundaries,
    grapheme_clusters,
)
from protodial.presentation._truncation import (
    PrefixSearchMetrics,
    abbreviate_graphemes,
)
from protodial.presentation.diagnostics.context import use_sink
from protodial.presentation.diagnostics.error_events import (
    operation_symbol,
    ordinary_error_event,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    HumanTruncationMetrics,
    JsonTruncationMetrics,
    StderrDiagnosticSink,
    _bounded_diagnostic_json_base,
    _display_width,
    _human_detail,
    _human_truncation_notice,
    _truncate_human,
    _truncate_json_with_bounded_base,
    _wrap_cells,
    abbreviate_terminal_text,
    format_duration,
    render_human,
    render_json,
    truncate_generic_human,
    truncate_rpc_failure_human,
)
from protodial.presentation.sanitization import escape_control_characters
from protodial.rpc_core.models import RpcFailure, RpcSummary
from protodial.schema.local import _emit_compiler_warnings
from protodial.schema.reflection import reflection_database


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_presentation_options_and_environment_precedence(monkeypatch) -> None:
    target = "grpc://localhost:50051/example.Service/Call"
    monkeypatch.setenv("PROTODIAL_DIAGNOSTIC_FORMAT", "json")
    from_environment = parse_arguments([target])
    overridden = parse_arguments(
        ["--diagnostic-format", "human", "--color", "never", target]
    )

    assert from_environment.diagnostics.diagnostic_format == "json"
    assert overridden.diagnostics.diagnostic_format == "human"
    assert overridden.diagnostics.color == "never"


def test_json_sink_is_ndjson_sequenced_typed_and_never_colored() -> None:
    stream = TtyBuffer()
    sink = StderrDiagnosticSink(
        verbosity=2,
        stream=stream,
        diagnostic_format="json",
        color="always",
    )
    sink.emit(
        DiagnosticEvent(
            "rpc.headers",
            "response headers received",
            metadata=(
                ("same", "one"),
                ("same", "two"),
                ("trace-bin", b"\x00\xff"),
                ("authorization", "secret"),
                ("ordinary", "literal value"),
            ),
        )
    )
    sink.emit(
        DiagnosticEvent(
            "input.invalid",
            "request contains a newline\nand an escape \x1b[2J",
            severity=DiagnosticSeverity.ERROR,
            code="E2101",
            spans=(DiagnosticSpan("--data", line=1, column=2),),
        )
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["schema"] == "protodial.diagnostic.v1"
    assert [first["sequence"], second["sequence"]] == [1, 2]
    assert [item["name"] for item in first["metadata"]] == [
        "same",
        "same",
        "trace-bin",
        "authorization",
        "ordinary",
    ]
    assert first["metadata"][2]["value"] == {
        "encoding": "base64",
        "data": "AP8=",
    }
    assert first["metadata"][3]["value"] == "secret"
    assert first["metadata"][4]["value"] == "literal value"
    assert second["message"].startswith("request contains a newline\n")
    assert "\x1b[" not in stream.getvalue()


def test_empty_metadata_phases_are_silent_only_for_human_output() -> None:
    events = (
        DiagnosticEvent("rpc.headers", level=0, metadata=()),
        DiagnosticEvent(
            "rpc.trailers",
            level=0,
            metadata=(("blank", " \t"), (" ", "visible")),
        ),
    )
    human_stream = io.StringIO()
    human_sink = StderrDiagnosticSink(0, stream=human_stream, color="never")
    json_stream = io.StringIO()
    json_sink = StderrDiagnosticSink(
        0, stream=json_stream, diagnostic_format="json"
    )

    for event in events:
        human_sink.emit(event)
        json_sink.emit(event)

    assert human_stream.getvalue() == ""
    assert human_sink.sequence == 0
    machine = [json.loads(line) for line in json_stream.getvalue().splitlines()]
    assert [item["event"] for item in machine] == [
        "rpc.headers",
        "rpc.trailers",
    ]
    assert [item["sequence"] for item in machine] == [1, 2]
    assert machine[1]["metadata"] == [
        {"name": "blank", "value": " \t"},
        {"name": " ", "value": "visible"},
    ]


def test_metadata_wire_blocks_wrap_safely_with_stable_continuations() -> None:
    event = DiagnosticEvent(
        "rpc.headers",
        metadata=(
            ("same\x1b[2J界", "first界 value " * 20),
            ("same\x1b[2J界", "second界 value " * 20),
            ("k" * 55, "界 tail"),
            ("trace-bin", b"\x00" * 80),
        ),
    )

    rendered = render_human(event, color=True, width=60)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()

    assert all(_display_width(line) <= 60 for line in lines)
    assert lines[-1] == "<"
    assert all(line.startswith("<") for line in lines)
    assert sum(line.startswith("<   ") for line in lines) >= 2
    assert plain.count(r"same\x1b[2J界:") == 2
    assert (
        plain.index("first") < plain.index("second") < plain.index("trace-bin")
    )
    assert "\x1b" not in plain


def test_extension_event_retains_arbitrary_context() -> None:
    event = DiagnosticEvent(
        "extension.event", "extension message", fields={"answer": 42}
    )
    json_stream = io.StringIO()
    StderrDiagnosticSink(1, stream=json_stream, diagnostic_format="json").emit(
        event
    )

    assert json.loads(json_stream.getvalue())["context"] == {"answer": 42}


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_large_rich_details_are_bounded_with_explicit_note(
    diagnostic_format: str,
) -> None:
    stream = io.StringIO()
    sink = StderrDiagnosticSink(
        0,
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="always",
    )
    sink.emit(
        DiagnosticEvent(
            "rpc.failed",
            "RPC failed with INTERNAL (13)",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            details=({"type": "example.Huge", "value": "x" * (70 * 1024)},),
        )
    )

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    assert "rich status truncated" in output
    if diagnostic_format == "json":
        value = json.loads(output)
        marker = value["details"][-1]
        assert marker["type"] == "protodial.truncated"
        assert marker["preview"].startswith('[{"type":"example.Huge"')
        original = json.dumps(
            [{"type": "example.Huge", "value": "x" * (70 * 1024)}],
            separators=(",", ":"),
        )
        assert marker["characters_omitted"] == (
            len(original) - len(marker["preview"])
        )
        assert "\x1b" not in output


def test_rich_detail_priority_survives_preceding_bulk_in_human_and_json() -> (
    None
):
    details = (
        {
            "type": "type.googleapis.com/google.rpc.DebugInfo",
            "detail": "debug " + "x" * (70 * 1024),
        },
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {
                    "field": "resource",
                    "description": "resource must not be empty",
                },
            ),
        },
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        causes=("validation failed",),
        details=details,
    )

    # The ordinary JSON API remains a lossless wire-order representation.
    assert [
        item["type"] for item in json.loads(render_json(event, 1))["details"]
    ] == [
        "type.googleapis.com/google.rpc.DebugInfo",
        "type.googleapis.com/google.rpc.BadRequest",
    ]

    human = io.StringIO()
    StderrDiagnosticSink(0, stream=human, color="never").emit(event)
    rendered = human.getvalue()
    assert rendered.index("ValidateRequest.resource") < rendered.index(
        "debug information:"
    )
    assert "INVALID_ARGUMENT (3)" in rendered
    assert "reason: validation failed" in rendered
    assert "call: `examples.errors.Validator/Validate`" in rendered

    machine = io.StringIO()
    StderrDiagnosticSink(0, stream=machine, diagnostic_format="json").emit(
        event
    )
    value = json.loads(machine.getvalue())
    assert value["details"][0]["type"].endswith("google.rpc.BadRequest")
    assert value["details"][0]["field_violations"][0]["field"] == "resource"
    marker = value["details"][-1]
    assert marker["type"] == "protodial.truncated"
    assert marker["omitted_detail_indexes"] == [0]
    assert marker["original_detail_count"] == 2
    assert value["context"]["grpc_status"]["name"] == "INVALID_ARGUMENT"
    assert value["context"]["method"] == "examples.errors.Validator/Validate"
    assert value["causes"] == ["validation failed"]


@pytest.mark.parametrize(
    "oversized_field",
    ["cause", "help", "metadata", "method", "note", "status", "target"],
)
def test_oversized_failure_fields_cannot_displace_actionable_json_detail(
    oversized_field: str,
) -> None:
    huge = "界" * (70 * 1024)
    fields = {
        "target": "localhost:50053",
        "method": "example.Validator/Validate",
        "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
    }
    causes: tuple[str, ...] = ()
    help_items: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    if oversized_field == "cause":
        causes = (huge,)
    elif oversized_field == "help":
        help_items = (huge,)
    elif oversized_field == "metadata":
        metadata = (("large-trailer", huge),)
    elif oversized_field == "method":
        fields["method"] = huge
    elif oversized_field == "note":
        notes = (huge,)
    elif oversized_field == "status":
        fields["grpc_status"] = {"name": huge, "number": 3}
    else:
        fields["target"] = huge

    debug = {
        "type": "type.googleapis.com/google.rpc.DebugInfo",
        "detail": huge,
    }
    bad_request = {
        "type": "type.googleapis.com/google.rpc.BadRequest",
        "field_violations": (
            {"field": "resource", "description": "must not be empty"},
        ),
    }
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields=fields,
        causes=causes,
        help=help_items,
        notes=notes,
        metadata=metadata,
        details=(debug, bad_request),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(event)

    output = stream.getvalue()
    value = json.loads(output)
    assert len(output.encode("utf-8")) < 64 * 1024
    assert value["details"][0] == {
        "type": "type.googleapis.com/google.rpc.BadRequest",
        "field_violations": [
            {"field": "resource", "description": "must not be empty"}
        ],
    }
    marker = value["details"][-1]
    assert marker["type"] == "protodial.truncated"
    assert marker["omitted_detail_indexes"] == [0]
    assert marker["original_detail_count"] == 2
    omitted = json.dumps([debug], ensure_ascii=True, separators=(",", ":"))
    assert marker["preview"] == omitted[: len(marker["preview"])]
    assert marker["characters_omitted"] == len(omitted) - len(marker["preview"])
    assert "method" in value["context"]
    assert "target" in value["context"]
    assert "grpc_status" in value["context"]
    if causes:
        assert value["causes"][0].endswith("…")
    if help_items:
        assert value["help"][0].endswith("…")
    if metadata:
        assert value["metadata"][0]["value"].endswith("…")
    if notes:
        assert value["notes"][0].endswith("…")


def test_json_sink_preserves_exact_content_boundary_then_truncates() -> None:
    details = (
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": "resource", "description": "required"},
            ),
        },
    )
    fields = {
        "target": "localhost:50053",
        "method": "example.Validator/Validate",
        "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
    }
    content_limit = 64 * 1024 - 2
    probe = DiagnosticEvent(
        "rpc.failed",
        "x",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields=fields,
        details=details,
    )
    padding = content_limit - len(render_json(probe, 1))
    exact = replace(probe, message="x" * (padding + 1))
    over = replace(exact, message=exact.message + "x")
    assert len(render_json(exact, 1).encode("utf-8")) == content_limit

    exact_stream = io.StringIO()
    StderrDiagnosticSink(0, stream=exact_stream, diagnostic_format="json").emit(
        exact
    )
    over_stream = io.StringIO()
    StderrDiagnosticSink(0, stream=over_stream, diagnostic_format="json").emit(
        over
    )

    exact_value = json.loads(exact_stream.getvalue())
    over_value = json.loads(over_stream.getvalue())
    assert len(exact_stream.getvalue().encode("utf-8")) == content_limit + 1
    assert "notes" not in exact_value
    assert exact_value["message"] == exact.message
    assert exact_value["details"][0]["type"].endswith("BadRequest")
    assert len(over_stream.getvalue().encode("utf-8")) < 64 * 1024
    assert "truncated" in over_value["notes"][-1]
    assert over_value["details"][0]["type"].endswith("BadRequest")


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_large_diagnostic_without_details_is_strictly_bounded(
    diagnostic_format: str,
) -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(
        0,
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="always",
    ).emit(
        DiagnosticEvent(
            "command.failed",
            "`" + "wide界e\u0301" * (16 * 1024) + "`",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
        )
    )

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    if diagnostic_format == "json":
        value = json.loads(output)
        assert value["event"] == "command.failed"
        assert "truncated" in value["notes"][0]
    else:
        assert "  note: diagnostic truncated;" in output
        assert "rich status truncated" not in output
        # Every escape introduced by styling is a complete SGR sequence.
        assert re.sub(r"\x1b\[[0-9;]*m", "", output).find("\x1b") == -1


def test_oversized_ordinary_metadata_uses_generic_truncation_wording() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="never").emit(
        DiagnosticEvent(
            "rpc.headers",
            level=0,
            metadata=(("ordinary", "x" * (70 * 1024)),),
        )
    )

    rendered = stream.getvalue()
    assert "  note: diagnostic truncated;" in rendered
    assert "rich status truncated" not in rendered
    assert len(rendered.encode("utf-8")) < 64 * 1024


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_large_rpc_failure_without_details_retains_essentials(
    diagnostic_format: str,
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "localhost:50053",
            "method": "example.Validator/Validate",
            "status": "INVALID_ARGUMENT",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
            "sent": 1,
            "received": 0,
            "sent_messages": 1,
            "received_messages": 0,
        },
        causes=("validation failed " + "x" * (70 * 1024),),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(
        0,
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="always",
    ).emit(event)

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    if diagnostic_format == "json":
        value = json.loads(output)
        assert value["context"]["grpc_status"] == {
            "name": "INVALID_ARGUMENT",
            "number": 3,
        }
        assert value["context"]["method"] == "example.Validator/Validate"
        assert value["causes"][0].startswith("validation failed")
    else:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
        assert "INVALID_ARGUMENT (3)" in plain
        assert "reason: validation failed" in plain
        assert "call: `example.Validator/Validate`" in plain
        assert "localhost:50053" not in plain
        assert re.sub(r"\x1b\[[0-9;]*m", "", output).find("\x1b") == -1


def test_colored_oversized_rpc_never_splits_an_ansi_sequence() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="always").emit(
        DiagnosticEvent(
            "rpc.failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            fields={"grpc_status": {"name": "X" * (70 * 1024), "number": 13}},
        )
    )

    output = stream.getvalue()
    assert len(output) < 64 * 1024
    assert len(output.encode("utf-8")) < 64 * 1024
    assert re.sub(r"\x1b\[[0-9;]*m", "", output).find("\x1b") == -1


@pytest.mark.parametrize("color", ["never", "always"])
@pytest.mark.parametrize("detail_kind", ["BadRequest", "DebugInfo"])
def test_large_human_rich_details_retain_essential_failure_context(
    color: str, detail_kind: str
) -> None:
    if detail_kind == "BadRequest":
        detail = {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": "resource", "description": "x" * (70 * 1024)},
            ),
        }
    else:
        detail = {
            "type": "type.googleapis.com/google.rpc.DebugInfo",
            "detail": "x" * (70 * 1024),
            "stack_entries": ("service.py:20",),
        }
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "localhost:50053",
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        causes=("validation failed",),
        details=(detail,),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color=color).emit(event)

    output = stream.getvalue()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    assert len(output) < 12 * 1024
    assert "rich status truncated" in plain
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in plain
    assert "  reason: validation failed" in plain
    assert "  call: `examples.errors.Validator/Validate`" in plain
    assert "localhost:50053" not in plain
    assert (
        "ValidateRequest.resource"
        if detail_kind == "BadRequest"
        else "debug information:"
    ) in plain
    assert (
        output.count("\x1b[") > 0
        if color == "always"
        else "\x1b[" not in output
    )


def test_large_failure_values_cannot_displace_other_essential_sections() -> (
    None
):
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "target-" + "t" * (70 * 1024),
            "method": "method-" + "m" * (70 * 1024),
            "grpc_status": {"name": "INTERNAL", "number": 13},
        },
        causes=("reason-" + "r" * (70 * 1024),),
        details=(
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "debug-" + "d" * (70 * 1024),
            },
        ),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="never").emit(event)

    rendered = stream.getvalue()
    assert len(rendered) < 12 * 1024
    assert "protodial: (E4103) INTERNAL (13)" in rendered
    assert "  reason: reason-" in rendered
    assert "  call: `method-" in rendered
    assert "target-" not in rendered
    assert "rich status truncated" in rendered


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


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.000999499, "999 us"),
        (0.0009995, "1 ms"),
        (0.999499, "999 ms"),
        (0.9995, "1 s"),
    ],
)
def test_duration_rounding_promotes_normalized_unit_boundaries(
    seconds: float, expected: str
) -> None:
    rendered = format_duration(seconds)

    assert rendered == expected
    assert "e+03" not in rendered


@pytest.mark.parametrize(
    "seconds", [-1.0, float("inf"), float("-inf"), float("nan")]
)
def test_duration_rejects_invalid_values_without_crashing(
    seconds: float,
) -> None:
    assert format_duration(seconds) == "unknown duration"


@pytest.mark.parametrize(
    ("delay", "expected"),
    [
        ("-5s", "unknown duration"),
        ("NaNs", "unknown duration"),
        ("after maintenance", "after maintenance"),
    ],
)
def test_retry_info_has_sane_invalid_and_arbitrary_text(
    delay: str, expected: str
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "UNAVAILABLE", "number": 14}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.RetryInfo",
                "retry_delay": delay,
            },
        ),
    )

    assert f"retry after {expected}" in render_human(event)


def test_nested_rich_detail_values_are_preserved_in_both_renderers() -> None:
    event = DiagnosticEvent(
        "extension.failed",
        "RPC failed with UNAUTHENTICATED (16)",
        severity=DiagnosticSeverity.ERROR,
        fields={"nested": {"password": "field secret"}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "TOKEN_EXPIRED",
                "domain": "example.test",
                "metadata": {
                    "authorization": "detail secret",
                    "ordinary": "visible",
                    "nested": {"api-token": "deep secret"},
                },
            },
        ),
    )
    human = io.StringIO()
    StderrDiagnosticSink(1, stream=human, color="never").emit(event)
    machine = io.StringIO()
    StderrDiagnosticSink(1, stream=machine, diagnostic_format="json").emit(
        event
    )

    rendered = human.getvalue()
    assert "error information:" in rendered
    assert "detail secret" in rendered
    assert "deep secret" in rendered
    assert "ordinary: visible" in rendered

    value = json.loads(machine.getvalue())
    assert value["context"]["nested"]["password"] == "field secret"
    metadata = value["details"][0]["metadata"]
    assert metadata["authorization"] == "detail secret"
    assert metadata["nested"]["api-token"] == "deep secret"
    assert metadata["ordinary"] == "visible"


def test_recursive_rich_detail_preserves_common_key_spellings() -> None:
    secrets = {
        "client_secret": "one",
        "api.token": "two",
        "access_token": "three",
        "db_password": "four",
        "passwordHash": "five",
        "credentials": "six",
    }
    event = DiagnosticEvent(
        "rpc.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        details=(
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "metadata": {"nested": secrets},
            },
        ),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(event)
    nested = json.loads(stream.getvalue())["details"][0]["metadata"]["nested"]

    assert nested == secrets


def test_human_color_policy_and_terminal_injection_safety() -> None:
    event = DiagnosticEvent(
        "command.failed",
        "bad\nforged\rline\x1b]0;owned\x07\u202evalue",
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
    )
    colored = TtyBuffer()
    StderrDiagnosticSink(
        1,
        stream=colored,
        color="always",
        environ={"NO_COLOR": "", "TERM": "dumb"},
    ).emit(event)
    plain = TtyBuffer()
    StderrDiagnosticSink(1, stream=plain, color="never").emit(event)

    assert colored.getvalue().startswith("\x1b[1;31mprotodial: (E2001)\x1b[0m ")
    assert "\x1b]0" not in colored.getvalue()
    assert "\\nforged\\rline\\x1b]0;owned\\x07\\u202evalue" in (
        colored.getvalue()
    )
    assert "\x1b" not in plain.getvalue()


@pytest.mark.parametrize(
    ("environ", "expected_color"),
    [
        ({"TERM": "xterm"}, True),
        ({"TERM": "dumb"}, False),
        ({"TERM": "xterm", "NO_COLOR": ""}, False),
        ({"TERM": "xterm", "NO_COLOR": "1"}, False),
    ],
)
def test_auto_color_uses_diagnostic_tty_and_environment(
    environ: dict[str, str], expected_color: bool
) -> None:
    stream = TtyBuffer()
    StderrDiagnosticSink(0, stream=stream, color="auto", environ=environ).emit(
        DiagnosticEvent(
            "command.failed",
            "failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
        )
    )
    assert ("\x1b[" in stream.getvalue()) is expected_color


def test_cli_usage_error_respects_bootstrap_json(capsys) -> None:
    assert cli.main(["--diagnostic-format", "json", "--not-an-option"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "usage.invalid"
    assert value["code"] == "E1001"
    assert value["severity"] == "error"


def test_unrecognized_argument_cannot_inject_a_replacement_parser_error(
    capsys,
) -> None:
    forged = "--unknown\nprotodial: error: forged\x1b[2J"
    assert (
        cli.main(
            [
                "--diagnostic-format",
                "json",
                "grpc://localhost/example.Echo/Call",
                forged,
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    value = json.loads(captured.err)
    assert value["event"] == "usage.invalid"
    assert value["message"].startswith("unrecognized arguments: --unknown")
    assert value["message"] != "forged\x1b[2J"


def test_bootstrap_ignores_values_of_other_options_and_double_dash() -> None:
    assert cli._bootstrap_policy(
        ["--data", "--diagnostic-format=json", "target"], {}
    ) == ("json", "auto", None)
    assert cli._bootstrap_policy(["--", "--diagnostic-format=json"], {}) == (
        "human",
        "auto",
        None,
    )
    assert cli._bootstrap_policy(
        ["--data", "--", "--diagnostic-format=json"], {}
    ) == ("human", "auto", None)
    assert cli._bootstrap_policy(
        ["--header", "--color=always", "--diagnostic-format=json"], {}
    ) == ("json", "always", None)


def test_usage_span_marks_only_the_invalid_attached_value(capsys) -> None:
    assert cli.main(["--color=rainbow"]) == 2
    error = capsys.readouterr().err

    assert "--> argument 1" in error
    assert "1 | --color=rainbow" in error
    caret_line = next(line for line in error.splitlines() if "^" in line)
    assert caret_line.count("^") == len("rainbow")
    assert caret_line.index("^") > caret_line.index("|") + len("--color=")


def test_source_span_expands_tabs_before_positioning_caret() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(1, stream=stream, color="never").emit(
        DiagnosticEvent(
            "input.invalid",
            "invalid input",
            severity=DiagnosticSeverity.ERROR,
            code="E2101",
            spans=(
                DiagnosticSpan(
                    "--data",
                    line=1,
                    column=2,
                    text="\tbad",
                    start=1,
                    end=4,
                    label="invalid token",
                ),
            ),
        )
    )

    source, caret = (
        line for line in stream.getvalue().splitlines() if "| " in line
    )
    assert source.endswith("|     bad")
    assert caret.endswith("|     ^^^ invalid token")


@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("text", ["x" * 100, "界" * 50, "e\u0301" * 100])
def test_source_insertion_at_end_keeps_caret_and_label_within_width(
    color: bool, text: str
) -> None:
    event = DiagnosticEvent(
        "input.invalid",
        "missing value",
        severity=DiagnosticSeverity.ERROR,
        code="E2101",
        spans=(
            DiagnosticSpan(
                "argument 1",
                line=1,
                text=text,
                start=len(text),
                end=len(text),
                label="insert the missing value here",
            ),
        ),
    )

    rendered = render_human(event, color=color, width=60)
    plain_lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", line) for line in rendered.splitlines()
    ]

    assert all(_display_width(line) <= 60 for line in plain_lines)
    caret_line = next(line for line in plain_lines if "^" in line)
    assert caret_line.count("^") == 1
    assert "insert the missing value here" in "\n".join(plain_lines)


def test_rpc_failure_prioritizes_status_reason_and_compacts_call_context() -> (
    None
):
    command = parse_arguments(["grpc://api.example:50051/example.Users/Create"])
    summary = RpcSummary(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="INVALID_ARGUMENT", value=(3, "unused")),
        details="request validation failed",
        sent_messages=1,
        received_messages=2,
        elapsed_seconds=0.0241,
    )
    event = cli._rpc_failure_event(
        command,
        RpcFailure(Exception("internal transport wrapper"), summary, False),
    )
    rendered = io.StringIO()
    StderrDiagnosticSink(0, stream=rendered, color="never").emit(event)

    assert rendered.getvalue().splitlines() == [
        "protodial: (E4103) INVALID_ARGUMENT (3) in 24.1 ms; "
        "sent 1 message; received 2 messages",
        "  reason: request validation failed",
        "  call: `example.Users/Create`",
    ]


def test_rpc_failure_does_not_render_target_without_a_method() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "api.example:50051",
            "grpc_status": {"name": "UNAVAILABLE", "number": 14},
        },
    )

    assert render_human(event).splitlines() == [
        "protodial: (E4103) UNAVAILABLE (14)"
    ]


def test_rpc_failure_places_rich_details_before_call_context() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        "supplied prose is not the headline",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": "localhost:50053",
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
            "elapsed_seconds": 0.00076,
            "sent_messages": 1,
            "received_messages": 0,
        },
        causes=("validation failed",),
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "resource must not be empty",
                    },
                ),
            },
        ),
    )

    assert render_human(event).splitlines() == [
        "ValidateRequest.resource: resource must not be empty",
        "protodial: (E4103) INVALID_ARGUMENT (3) in 760 us; "
        "sent 1 message; received 0 messages",
        "  reason: validation failed",
        "  call: `examples.errors.Validator/Validate`",
    ]


def test_rpc_failure_color_distinguishes_field_label_and_method() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "method": "examples.errors.Validator/Validate",
            "request_type": "examples.errors.ValidateRequest",
            "grpc_status": {"name": "INVALID_ARGUMENT", "number": 3},
        },
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": "resource",
                        "description": "resource must not be empty",
                    },
                ),
            },
        ),
    )

    rendered = render_human(event, color=True)

    assert (
        "\x1b[1mValidateRequest.resource\x1b[0m: resource must not be empty"
        in rendered
    )
    assert "\x1b[1mcall\x1b[0m:" in rendered
    assert "\x1b[34m`examples.errors.Validator/Validate`\x1b[0m" in rendered
    assert "\x1b[1m`examples.errors.Validator/Validate`" not in rendered


def test_transport_failure_names_target_and_preserves_actionable_cause() -> (
    None
):
    command = parse_arguments(["grpc://api.example:50051/example.Users/Create"])
    event = ordinary_error_event(
        ConnectionError("connection deadline exceeded"), command
    )
    rendered = io.StringIO()
    StderrDiagnosticSink(0, stream=rendered, color="never").emit(event)

    assert rendered.getvalue().splitlines() == [
        "protodial: (E3002) could not connect to `api.example:50051`",
        "  reason: connection deadline exceeded",
        "  help: check the target, network, TLS, and timeout settings",
    ]


def test_invalid_diagnostic_environment_has_plain_usage_error(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PROTODIAL_DIAGNOSTIC_FORMAT", "xml")
    assert cli.main(["--version"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("protodial: (E1002) ")
    assert "\x1b" not in captured.err


def test_json_mode_suppresses_native_grpc_stderr_before_runtime_load(
    monkeypatch, capsys
) -> None:
    observed: list[str | None] = []
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=Exception), protobuf_errors=()
    )

    def load_runtime() -> object:
        observed.append(os.environ.get("GRPC_VERBOSITY"))
        return runtime

    monkeypatch.setenv("GRPC_VERBOSITY", "DEBUG")
    monkeypatch.setattr(cli, "load_runtime", load_runtime)
    monkeypatch.setattr(cli, "describe", lambda *_args: None)

    assert (
        cli.main(
            [
                "describe",
                "--diagnostic-format",
                "json",
                "localhost:50051",
                "example.Message",
            ]
        )
        == 0
    )
    assert observed == ["NONE"]
    assert capsys.readouterr().err == ""


def test_human_mode_suppresses_native_logs_and_escapes_malicious_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[str | None] = []
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=type("RpcError", (Exception,), {})),
        protobuf_errors=(),
    )

    def load_runtime() -> object:
        observed.append(os.environ.get("GRPC_VERBOSITY"))
        return runtime

    def fail(*_args: object) -> None:
        raise ConnectionError("dial failed")

    monkeypatch.setenv("GRPC_VERBOSITY", "DEBUG")
    monkeypatch.setattr(cli, "load_runtime", load_runtime)
    monkeypatch.setattr(cli, "invoke", fail)
    target = "grpc://bad\x1bX.example:50051/example.Echo/Call"

    assert cli.main(["--color", "never", target]) == 1
    captured = capsys.readouterr()
    assert observed == ["NONE"]
    assert captured.out == ""
    assert "\\x1bX" in captured.err
    assert "\x1b" not in captured.err


def test_cli_malformed_literal_has_rust_style_source_span(
    monkeypatch, capsys
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise MessageParseError(
            "invalid JSON request 1 at line 2, column 3: expected value"
        )

    monkeypatch.setattr(cli, "invoke", fail)
    exit_code = cli.main(
        [
            "--color",
            "never",
            "--data",
            "{\n  bad\n}",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("protodial: (E2101) ")
    assert "--> --data:2:3" in captured.err
    assert "2 |   bad" in captured.err
    assert "^ invalid input" in captured.err


def test_cli_json_decoder_recursion_error_is_an_input_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    raw_decode = json.JSONDecoder.raw_decode

    def fail_decode(
        decoder: json.JSONDecoder, value: str, idx: int = 0
    ) -> tuple[object, int]:
        if value == "[":
            raise RecursionError("maximum recursion depth exceeded")
        return raw_decode(decoder, value, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", fail_decode)

    def invoke(*_args: object) -> None:
        list(
            iter_json_values(
                io.BytesIO(b"["),
                SimpleNamespace(max_input_buffer=16),
            )
        )

    monkeypatch.setattr(cli, "invoke", invoke)
    exit_code = cli.main(
        [
            "--diagnostic-format",
            "json",
            "--data",
            "[",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert exit_code == 1
    assert diagnostic["event"] == "input.invalid"
    assert diagnostic["code"] == "E2101"
    assert diagnostic["causes"] == [
        "invalid JSON request 1 at line 1, column 1: JSON nesting is too deep"
    ]


@pytest.mark.parametrize(
    ("data", "column"),
    [("{", 2), ('{"name":', 9), ("[1,", 4)],
)
def test_cli_malformed_literal_at_eof_has_bounded_insertion_span(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: str,
    column: int,
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise MessageParseError(
            f"invalid JSON request 1 at line 1, column {column}: expected value"
        )

    monkeypatch.setattr(cli, "invoke", fail)
    exit_code = cli.main(
        [
            "--color",
            "never",
            "--data",
            data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("protodial: (E2101) ")
    assert f"--> --data:1:{column}" in captured.err
    assert f"1 | {data}" in captured.err
    assert "^ invalid input" in captured.err


@pytest.mark.parametrize("source_kind", ["literal", "file"])
def test_textproto_parser_position_locates_multiline_input(
    tmp_path: Path, source_kind: str
) -> None:
    data = 'name: "Ada"\nbroken'
    if source_kind == "file":
        source_path = tmp_path / "request.textproto"
        source_path.write_text(data, encoding="utf-8")
        configured_data = f"@{source_path}"
        expected_source = str(source_path)
    else:
        configured_data = data
        expected_source = "--data"
    command = parse_arguments(
        [
            "--input-format",
            "text",
            "--data",
            configured_data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(
        MessageParseError(
            "invalid text request 1: 2:1 : Message type has no field named "
            '"broken".'
        ),
        command,
    )

    assert event.spans == (
        DiagnosticSpan(
            expected_source,
            line=2,
            column=1,
            text="broken",
            start=0,
            end=1,
            label="invalid input",
        ),
    )


def test_textproto_position_parser_ignores_unrelated_colons() -> None:
    data = 'name: "Ada"\nbroken'
    command = parse_arguments(
        [
            "--input-format",
            "text",
            "--data",
            data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(
        MessageParseError("invalid text request 1: could not parse value: 2:1"),
        command,
    )

    assert event.spans[0].line is None
    assert event.spans[0].column is None
    assert event.spans[0].text == 'name: "Ada"'


@pytest.mark.parametrize("input_format", ["text", "protobuf", "raw"])
def test_non_json_parse_errors_omit_json_only_remediation(
    input_format: str,
) -> None:
    command = parse_arguments(
        [
            "--input-format",
            input_format,
            "--data",
            "invalid",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(MessageParseError("invalid request"), command)
    machine = json.loads(render_json(event, 1))

    assert event.help == ()
    assert "validate JSON" not in render_human(event)
    assert "help" not in machine


def test_json_parse_error_retains_json_remediation() -> None:
    command = parse_arguments(
        [
            "--data",
            "invalid",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(MessageParseError("invalid request"), command)

    assert event.help == ("validate JSON input with `jq .`",)
    assert json.loads(render_json(event, 1))["help"] == [
        "validate JSON input with `jq .`"
    ]


def test_valid_json_schema_error_names_expected_protojson_message() -> None:
    command = parse_arguments(
        [
            "--data",
            '{"a": "b"}',
            "grpc://localhost:50051/example.Service/Call",
        ]
    )
    error = JsonMessageSchemaError(
        1,
        "example.Request",
        'Message type "example.Request" has no field named "a"',
    )

    event = ordinary_error_event(error, command)
    human = render_human(event)
    machine = json.loads(render_json(event, 1))

    assert event.message == (
        "request body is valid JSON but does not match the ProtoJSON schema "
        "for example.Request"
    )
    assert event.causes == (
        'request 1: Message type "example.Request" has no field named "a"',
    )
    assert event.help == (
        "match field names and values to the example.Request schema",
    )
    assert event.fields == {
        "kind": "protojson_schema",
        "request_type": "example.Request",
        "request_index": 1,
    }
    assert "validate JSON input with `jq .`" not in human
    assert machine["message"] == event.message
    assert machine["causes"] == list(event.causes)

    narrow_lines = render_human(event, width=40).splitlines()
    assert narrow_lines[0] == f"protodial: (E2101) {event.message}"
    reason_line = next(
        line for line in narrow_lines if line.startswith("  reason:")
    )
    assert reason_line == (
        f"  reason: {event.causes[0]}"
    )


@pytest.mark.parametrize("data", ["-", "@-"])
def test_cli_malformed_stdin_forms_have_stdin_source_span(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: str,
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise MessageParseError(
            "invalid JSON request 1 at line 2, column 3: expected value"
        )

    monkeypatch.setattr(cli, "invoke", fail)
    exit_code = cli.main(
        [
            "--color",
            "never",
            "--data",
            data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "--> <stdin>:2:3" in captured.err


def test_invalid_proto_stderr_is_one_centralized_ndjson_event(
    tmp_path, capfd
) -> None:
    proto = tmp_path / "invalid.proto"
    proto.write_text("this is not protobuf\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["event"] == "command.failed"
    assert value["message"] == "proto compilation failed"
    assert any("compiler:" in note for note in value["notes"])


def test_invalid_proto_native_stderr_uses_human_diagnostic_grammar(
    tmp_path, capfd
) -> None:
    proto = tmp_path / "invalid-human.proto"
    proto.write_text("not a valid proto\n", encoding="utf-8")

    exit_code = cli.main(
        ["list", "--color", "never", "--proto", str(proto), "unused.invalid"]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith(
        "protodial: (E2001) proto compilation failed\n"
    )
    assert f"--> {proto}:1:1" in captured.err.replace("\n      ", "")
    assert "Expected top-level statement" in captured.err
    assert "  note: compiler:" in captured.err
    assert not any(
        line.startswith(("WARNING:", "W0000", str(proto)))
        for line in captured.err.splitlines()
    )


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_proto_compiler_eof_location_is_a_bounded_insertion_span(
    tmp_path: Path, capfd, diagnostic_format: str
) -> None:
    proto = tmp_path / "unterminated.proto"
    proto.write_text('syntax = "proto3";\nmessage X {', encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--color",
            "never",
            "--diagnostic-format",
            diagnostic_format,
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Traceback" not in captured.err
    if diagnostic_format == "json":
        value = json.loads(captured.err)
        assert (value["code"], value["event"]) == ("E2001", "command.failed")
        assert value["spans"] == [
            {
                "source": str(proto),
                "line": 2,
                "column": 12,
                "text": "message X {",
                "start": 11,
                "end": 11,
                "label": "invalid schema input",
            }
        ]
    else:
        assert captured.err.startswith(
            "protodial: (E2001) proto compilation failed\n"
        )
        assert f"--> {proto}:2:12" in captured.err.replace("\n      ", "")
        assert "message X {" in captured.err


@pytest.mark.parametrize("character", ["å", "😀"])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_proto_compiler_byte_columns_select_character_after_unicode(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    character: str,
    diagnostic_format: str,
) -> None:
    proto = tmp_path / "unicode-location.proto"
    source_line = (
        f'message X {{ string ok = 1 [json_name = "{character}"] nope; }}'
    )
    proto.write_text(f'syntax = "proto3";\n{source_line}\n', encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--color",
            "never",
            "--diagnostic-format",
            diagnostic_format,
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    intended = source_line.index("nope")
    if diagnostic_format == "json":
        value = json.loads(captured.err)
        span = value["spans"][0]
        assert span["text"][span["start"] : span["end"]] == "n"
        assert (span["start"], span["end"]) == (intended, intended + 1)
    else:
        lines = captured.err.splitlines()
        source_index = lines.index(f" 2 | {source_line}")
        marker = lines[source_index + 1]
        marker_indent = _display_width(source_line[:intended])
        assert marker == (f"   | {' ' * marker_indent}^ invalid schema input")


@pytest.mark.parametrize("character", ["å", "😀"])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_proto_warning_byte_columns_select_character_after_unicode(
    tmp_path: Path, character: str, diagnostic_format: str
) -> None:
    proto = tmp_path / "warning-location.proto"
    source_line = f'option java_package = "{character}"; unused'
    proto.write_text(source_line + "\n", encoding="utf-8")
    intended = source_line.index("unused")
    byte_column = len(source_line[:intended].encode("utf-8")) + 1
    stream = io.StringIO()
    sink = StderrDiagnosticSink(
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="never",
    )

    with use_sink(sink):
        _emit_compiler_warnings(
            (f"{proto}:1:{byte_column}: synthetic compiler warning",),
            (tmp_path,),
        )

    rendered = stream.getvalue()
    if diagnostic_format == "json":
        value = json.loads(rendered)
        span = value["spans"][0]
        assert span["text"][span["start"] : span["end"]] == "u"
        assert (span["start"], span["end"]) == (intended, intended + 1)
    else:
        lines = rendered.splitlines()
        source_index = lines.index(f" 1 | {source_line}")
        marker = lines[source_index + 1]
        marker_indent = _display_width(source_line[:intended])
        assert marker == f"   | {' ' * marker_indent}^ compiler warning"


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"",
        descriptor_pb2.FileDescriptorProto(
            name="invalid.proto", syntax="invalid"
        ).SerializeToString(),
    ],
)
def test_invalid_reflection_descriptor_is_a_sanitized_command_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    diagnostic_format: str,
    payload: bytes,
) -> None:
    class FakeRpcError(Exception):
        pass

    class StatusCode:
        UNIMPLEMENTED = SimpleNamespace(value=(12, "unimplemented"))

    grpc_module = SimpleNamespace(
        RpcError=FakeRpcError,
        StatusCode=StatusCode,
    )

    class Call:
        def __init__(self, response: object) -> None:
            self.response = response
            self.used = False

        def __iter__(self) -> Call:
            return self

        def __next__(self) -> object:
            if self.used:
                raise StopIteration
            self.used = True
            return self.response

        def cancel(self) -> None:
            pass

    class Channel:
        def stream_stream(
            self,
            _path: str,
            *,
            response_deserializer: object,
            **_kwargs: object,
        ) -> object:
            deserialize = response_deserializer
            assert callable(deserialize)

            def rpc(requests: object, **_call_kwargs: object) -> Call:
                request = next(iter(requests))  # type: ignore[arg-type]
                response = reflection_pb2.ServerReflectionResponse()
                response.original_request.ParseFromString(
                    request.SerializeToString()
                )
                response.file_descriptor_response.file_descriptor_proto.append(
                    payload
                )
                return Call(deserialize(response.SerializeToString()))

            return rpc

    def fail(_command: object, _grpc: object) -> None:
        database = reflection_database(Channel(), [], 1.0, grpc_module)
        database.query(file_by_filename="corrupt.proto")

    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda: SimpleNamespace(grpc=grpc_module, protobuf_errors=()),
    )
    monkeypatch.setattr(cli, "invoke", fail)

    assert (
        cli.main(
            [
                "--diagnostic-format",
                diagnostic_format,
                "grpc://localhost:50051/example.API/Call",
            ]
        )
        == 1
    )
    rendered = capsys.readouterr().err
    assert "reflection protocol error: invalid file descriptor payload" in (
        rendered
    )
    assert "DecodeError" not in rendered
    assert "Traceback" not in rendered
    assert "E9001" not in rendered
    if diagnostic_format == "json":
        value = json.loads(rendered)
        assert (value["event"], value["code"]) == (
            "command.failed",
            "E2001",
        )
    else:
        assert rendered.startswith("protodial: (E2001)")


def test_successful_proto_warnings_are_centralized_without_losing_context(
    tmp_path, capfd
) -> None:
    proto = tmp_path / "warning.proto"
    proto.write_text("package example; message Request {}\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["event"] == "schema.compiler_warning"
    assert value["severity"] == "warning"
    assert value["code"] == "W2001"
    assert value["message"] == "proto source does not declare its syntax"
    assert "syntax" in value["causes"][0].lower()
    assert value["spans"][0]["source"] == str(proto)
    assert value["spans"][0]["line"] == 1
    assert value["spans"][0]["text"] == proto.read_text().rstrip("\n")
    assert "proto3" in value["help"][0]


def test_missing_grpcio_tools_is_actionable_without_starting_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd
) -> None:
    import protodial.schema.local as local_schema

    proto = tmp_path / "schema.proto"
    proto.write_text('syntax = "proto3"; package example;\n')
    original_find_spec = local_schema.importlib.util.find_spec

    def find_spec(name: str):
        if name == "grpc_tools.protoc":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(local_schema.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(
        local_schema.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("protoc subprocess was started"),
    )

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "dependency.unavailable"
    assert value["code"] == "E8001"
    assert "grpcio-tools" in value["message"]
    assert "pip install grpcio-tools" in value["message"]
    assert "ModuleNotFoundError" not in captured.err
    assert "Traceback" not in captured.err


def test_malformed_protoset_is_a_located_schema_input_error(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    protoset = tmp_path / "broken.pb"
    protoset.write_bytes(b"\xff\xff\xff")

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--protoset",
            str(protoset),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    value = json.loads(captured.err)
    assert value["event"] == "schema.input_invalid"
    assert value["code"] == "E2002"
    assert value["spans"] == [
        {"source": str(protoset), "label": "invalid schema input"}
    ]
    assert "descriptor_set_out" in value["help"][0]


def test_semantically_invalid_protoset_is_a_located_schema_input_error(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    protoset = tmp_path / "invalid.pb"
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="invalid.proto",
        package="invalid",
        syntax="proto3",
        dependency=["missing.proto"],
    )
    protoset.write_bytes(
        descriptor_pb2.FileDescriptorSet(file=[file_proto]).SerializeToString()
    )

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--protoset",
            str(protoset),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "schema.input_invalid"
    assert value["code"] == "E2002"
    assert value["spans"] == [
        {"source": str(protoset), "label": "invalid schema input"}
    ]
    assert "missing.proto" in value["causes"][0]


@pytest.mark.parametrize("operation", ["list", "describe"])
def test_discovery_rpc_header_is_a_structured_usage_error(
    operation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    operands = (
        ["localhost:50051"]
        if operation == "list"
        else ["localhost:50051", "example.Message"]
    )
    assert (
        cli.main(
            [
                operation,
                "--diagnostic-format",
                "json",
                "--rpc-header",
                "x:value",
                *operands,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "usage.invalid"
    assert value["code"] == "E1001"


def test_missing_runtime_dependencies_use_bootstrapped_json_diagnostic() -> (
    None
):
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "from protodial.cli import main; "
                "raise SystemExit(main(['list', "
                "'--diagnostic-format', 'json', 'localhost:1']))"
            ),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    value = json.loads(completed.stderr)
    assert value["event"] == "dependency.unavailable"
    assert value["code"] == "E8001"
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("module", "distribution"),
    [
        ("cryptography.hazmat.primitives", "cryptography"),
        ("grpc_reflection.v1alpha", "grpcio-reflection"),
    ],
)
def test_missing_declared_dependency_is_actionable(
    module: str,
    distribution: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rpc_error = type("RpcError", (Exception,), {})
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=rpc_error), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    monkeypatch.setattr(cli, "describe", fail)

    assert (
        cli.main(
            [
                "describe",
                "--diagnostic-format",
                "json",
                "localhost:50051",
                "example.Echo",
            ]
        )
        == 1
    )
    value = json.loads(capsys.readouterr().err)
    assert value["event"] == "dependency.unavailable"
    assert value["code"] == "E8001"
    assert distribution in value["message"]


def test_closed_stderr_preserves_usage_exit_in_process() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; os.close(2); "
                "from protodial.cli import main; "
                "raise SystemExit(main(['--not-an-option']))"
            ),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_closed_stderr_preserves_direct_main_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedStderr:
        def write(self, _value: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stderr", ClosedStderr())

    assert cli.main(["--not-an-option"]) == 2


def test_verbose_local_discovery_does_not_claim_a_connection(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    proto = tmp_path / "offline.proto"
    proto.write_text(
        'syntax = "proto3"; package example; service Offline {}\n',
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "list",
                "-vv",
                "--diagnostic-format",
                "json",
                "--proto",
                str(proto),
                "unused.invalid",
            ]
        )
        == 0
    )
    captured = capfd.readouterr()
    events = [json.loads(line)["event"] for line in captured.err.splitlines()]
    assert captured.out == "example.Offline\n"
    assert "schema.source" in events
    assert "transport.connecting" not in events
    assert "transport.connected" not in events


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_runtime_initialization_defects_are_structured_without_traceback(
    diagnostic_format: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_runtime() -> object:
        raise RuntimeError("runtime initialization defect")

    monkeypatch.setattr(cli, "load_runtime", fail_runtime)

    assert (
        cli.main(
            [
                "describe",
                "--diagnostic-format",
                diagnostic_format,
                "localhost:50051",
                "example.Echo",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "E9001" in captured.err
    assert "runtime initialization defect" in captured.err
    assert "Traceback" not in captured.err


def test_runtime_initialization_interrupt_maps_to_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_runtime() -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "load_runtime", interrupt_runtime)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 130


def test_missing_option_value_still_bootstraps_following_json_policy(
    capsys,
) -> None:
    assert (
        cli.main(
            [
                "--data",
                "--diagnostic-format",
                "json",
                "grpc://h/S/M",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["event"] == "usage.invalid"


def test_repeated_invalid_option_span_marks_the_rejected_occurrence(
    capsys,
) -> None:
    assert cli.main(["--color", "never", "--color", "rainbow"]) == 2
    rendered = capsys.readouterr().err
    assert "--> argument 4" in rendered
    assert "--> argument 2" not in rendered
    assert "1 | rainbow" in rendered


@pytest.mark.parametrize(
    ("arguments", "source", "text", "start", "end"),
    [
        (
            ["--color=rainbow", "--color=never"],
            "argument 3",
            "--color=rainbow",
            len("--color="),
            len("--color=rainbow"),
        ),
        (
            ["--color=always", "--color=rainbow"],
            "argument 4",
            "--color=rainbow",
            len("--color="),
            len("--color=rainbow"),
        ),
        (
            ["--color", "rainbow", "--color", "rainbow"],
            "argument 4",
            "rainbow",
            0,
            len("rainbow"),
        ),
    ],
)
def test_choice_span_matches_first_rejected_value_and_spelling(
    arguments: list[str],
    source: str,
    text: str,
    start: int,
    end: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--diagnostic-format", "json", *arguments]) == 2
    value = json.loads(capsys.readouterr().err)
    assert value["spans"] == [
        {
            "source": source,
            "text": text,
            "start": start,
            "end": end,
            "label": "invalid value",
        }
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--diagnostic-format=xml", "--diagnostic-format=json"],
        ["--diagnostic-format=json", "--diagnostic-format", "xml"],
    ],
)
def test_any_visible_invalid_diagnostic_format_is_one_plain_e1002(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 2
    rendered = capsys.readouterr().err
    assert rendered.count("protodial: (E1002)") == 1
    assert "\x1b" not in rendered
    assert not rendered.lstrip().startswith("{")
    caret_span = next(line for line in rendered.splitlines() if "^" in line)
    assert caret_span.count("^") == len("xml")


def test_invalid_diagnostic_format_after_double_dash_is_not_bootstrapped() -> (
    None
):
    assert cli._bootstrap_policy(
        ["--diagnostic-format=json", "--", "--diagnostic-format=xml"], {}
    ) == ("json", "auto", None)


def test_semantic_context_is_strict_at_construction_and_emit() -> None:
    with pytest.raises(ValueError, match="unknown context key.*extra"):
        DiagnosticEvent(
            "rpc.completed",
            fields={
                "status": "OK",
                "sent": 1,
                "received": 1,
                "extra": "ambiguous",
            },
        )
    with pytest.raises(TypeError, match="'sent'.*integer"):
        DiagnosticEvent(
            "rpc.completed",
            fields={"status": "OK", "sent": True, "received": 1},
        )

    context = {"status": "OK", "sent": 1, "received": 1}
    event = DiagnosticEvent("rpc.completed", fields=context)
    context["elapsed_seconds"] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        StderrDiagnosticSink(
            1, stream=io.StringIO(), diagnostic_format="json"
        ).emit(event)


@pytest.mark.parametrize(
    ("event_name", "fields"),
    [
        (
            "rpc.completed",
            {"status": "OK", "sent": -1, "received": 0},
        ),
        (
            "rpc.failed",
            {"elapsed_seconds": -0.01},
        ),
        (
            "rpc.failed",
            {"received_messages": -1},
        ),
        (
            "rpc.lifecycle.completed",
            {"sent": 0, "received": -1},
        ),
    ],
)
def test_known_elapsed_and_counter_fields_reject_negative_values(
    event_name: str, fields: dict[str, object]
) -> None:
    with pytest.raises(TypeError, match="non-negative"):
        DiagnosticEvent(event_name, fields=fields)  # type: ignore[arg-type]


def test_rpc_terminal_machine_context_has_additive_compatible_aliases() -> None:
    command = parse_arguments(["grpc://api.example:50051/example.Users/Create"])
    summary = RpcSummary(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="INVALID_ARGUMENT", value=(3, "unused")),
        details="validation failed",
        sent_messages=1,
        received_messages=0,
        elapsed_seconds=0.5,
    )
    failed = cli._rpc_failure_event(
        command, RpcFailure(Exception("failure"), summary, False)
    )
    failed_stream = io.StringIO()
    StderrDiagnosticSink(
        0, stream=failed_stream, diagnostic_format="json"
    ).emit(failed)
    failed_value = json.loads(failed_stream.getvalue())

    assert failed_value["context"]["status"] == "INVALID_ARGUMENT"
    assert failed_value["context"]["target"] == "api.example:50051"
    assert failed_value["context"]["grpc_status"] == {
        "name": "INVALID_ARGUMENT",
        "number": 3,
    }
    assert failed_value["context"]["sent"] == 1
    assert failed_value["context"]["received"] == 0
    assert failed_value["context"]["sent_messages"] == 1
    assert failed_value["context"]["received_messages"] == 0


def test_rpc_failed_machine_message_is_semantic_when_prose_is_absent() -> None:
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, diagnostic_format="json").emit(
        DiagnosticEvent(
            "rpc.failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            fields={
                "method": "example.Users/Create",
                "grpc_status": {"name": "PERMISSION_DENIED", "number": 7},
            },
        )
    )

    value = json.loads(stream.getvalue())
    assert value["message"] == (
        "RPC `example.Users/Create` failed with PERMISSION_DENIED (7)"
    )


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


class _CountingProperties(Sequence[int]):
    def __init__(self, values: Sequence[int]) -> None:
        self._values = tuple(values)
        self.reads = 0

    @overload
    def __getitem__(self, index: int) -> int: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[int, ...]: ...

    def __getitem__(self, index: int | slice) -> int | tuple[int, ...]:
        if isinstance(index, slice):
            values = self._values[index]
            self.reads += len(values)
            return values
        self.reads += 1
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)


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


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan")],
)
def test_ndjson_rejects_nonfinite_values_before_serialization(
    value: float,
) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        DiagnosticEvent("extension.event", fields={"value": value})


@pytest.mark.parametrize(
    ("message", "expected_help", "expected_label"),
    [
        (
            "symbol 'Missing' not found; nearby: example.Present",
            "protodial list TARGET",
            "unknown schema symbol",
        ),
        (
            "ambiguous symbol 'Missing'; choose one of: a.Missing, b.Missing",
            "fully qualified symbol",
            "ambiguous schema symbol",
        ),
    ],
)
def test_symbol_errors_point_to_operand_and_offer_actionable_help(
    message: str,
    expected_help: str,
    expected_label: str,
) -> None:
    argv = ("describe", "localhost:50051", "Missing")
    command = parse_arguments(argv)
    event = ordinary_error_event(CliError(message), command, argv)

    assert event.spans == (
        DiagnosticSpan(
            "argument 3",
            text="Missing",
            start=0,
            end=7,
            label=expected_label,
        ),
    )
    assert expected_help in event.help[0]


@pytest.mark.parametrize(
    ("argv", "message", "label", "help_text"),
    [
        (
            ("list", "localhost:50051", "example.Message"),
            "'example.Message' does not name a service",
            "expected a service",
            "protodial list TARGET",
        ),
        (
            ("grpc://localhost:50051/example.Service/Message",),
            "'example.Service/Message' does not name a method",
            "expected a method",
            "SERVICE/METHOD",
        ),
    ],
)
def test_wrong_kind_operands_are_located_schema_errors(
    argv: tuple[str, ...], message: str, label: str, help_text: str
) -> None:
    command = parse_arguments(argv)
    event = ordinary_error_event(CliError(message), command, argv)

    assert event.code == "E2004"
    assert len(event.spans) == 1
    span = event.spans[0]
    assert span.text is not None
    assert span.text[span.start : span.end] == operation_symbol(command)
    assert span.label == label
    assert help_text in event.help[0]


def test_verbose_human_events_render_all_semantic_context() -> None:
    events = (
        DiagnosticEvent(
            "transport.connecting",
            level=2,
            fields={"target": "api.test:443", "transport": "verified"},
        ),
        DiagnosticEvent(
            "rpc.resolved",
            level=1,
            fields={
                "method": "example.Chat/Watch",
                "cardinality": "unary-stream",
                "request_type": "example.WatchRequest",
                "response_type": "example.WatchResponse",
            },
        ),
        DiagnosticEvent(
            "rpc.started",
            level=2,
            fields={"method": "example.Chat/Watch", "compression": "gzip"},
        ),
        DiagnosticEvent(
            "rpc.lifecycle.completed",
            "RPC lifecycle completed",
            level=2,
            fields={"sent": 1, "received": 3},
        ),
    )
    stream = io.StringIO()
    sink = StderrDiagnosticSink(2, stream=stream, color="never")
    for event in events:
        sink.emit(event)

    rendered = stream.getvalue()
    assert "using verified transport" in rendered
    assert "unary-stream" in rendered
    assert "`example.WatchRequest`" in rendered
    assert "`example.WatchResponse`" in rendered
    assert "using gzip compression" in rendered
    assert "sent 1 message; received 3 messages" in rendered


def test_unknown_rich_detail_keeps_its_decode_warning_in_human_and_json() -> (
    None
):
    event = DiagnosticEvent(
        "rpc.failed",
        "RPC failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        details=(
            {
                "type": "type.example/Unknown",
                "value": b"opaque",
                "warning": "unknown message type",
            },
        ),
    )
    human = io.StringIO()
    StderrDiagnosticSink(0, stream=human, color="never").emit(event)
    machine = io.StringIO()
    StderrDiagnosticSink(0, stream=machine, diagnostic_format="json").emit(
        event
    )

    assert (
        "`type.example/Unknown` detail: unknown message type"
        in human.getvalue()
    )
    assert json.loads(machine.getvalue())["details"][0]["warning"] == (
        "unknown message type"
    )


def test_location_only_span_has_one_empty_gutter_not_two() -> None:
    rendered = io.StringIO()
    StderrDiagnosticSink(0, stream=rendered, color="never").emit(
        DiagnosticEvent(
            "command.failed",
            "failed",
            level=0,
            severity=DiagnosticSeverity.ERROR,
            code="E2001",
            spans=(DiagnosticSpan("config.proto:7"),),
        )
    )

    assert rendered.getvalue().splitlines() == [
        "protodial: (E2001) failed",
        "  --> config.proto:7",
        "   |",
    ]


def test_color_styles_identifiers_in_all_sections() -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        "RPC `example.Call` failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        causes=("server rejected `request.id`",),
        help=("inspect `request.id`",),
        details=(
            {
                "type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "DENIED",
                "domain": "auth.test",
                "metadata": {"client_secret": "hidden"},
            },
        ),
    )
    stream = io.StringIO()
    StderrDiagnosticSink(0, stream=stream, color="always").emit(event)
    rendered = stream.getvalue()

    assert "\x1b[1m`request.id`\x1b[0m" in rendered
    assert "\x1b[1m`DENIED`\x1b[0m" in rendered
    assert "\x1b[1m`auth.test`\x1b[0m" in rendered
    assert "client_secret: hidden" in rendered


@pytest.mark.parametrize(
    "severity", [DiagnosticSeverity.ERROR, DiagnosticSeverity.WARNING]
)
@pytest.mark.parametrize("color", [False, True])
def test_human_headline_sanitizes_diagnostic_code(
    severity: DiagnosticSeverity, color: bool
) -> None:
    malicious_code = "E0000\n\x1b[31mforged"
    rendered = render_human(
        DiagnosticEvent(
            "extension.event",
            "safe headline",
            severity=severity,
            code=malicious_code,
        ),
        color=color,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert plain.count("\n") == 0
    assert "E0000\\n\\x1b[31mforged" in plain
    assert "\x1b[31mforged" not in rendered


@pytest.mark.parametrize(
    "severity", [DiagnosticSeverity.ERROR, DiagnosticSeverity.WARNING]
)
@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("width", [60, 79, 120])
def test_long_extension_code_uses_cell_safe_stacked_headline(
    severity: DiagnosticSeverity, color: bool, width: int
) -> None:
    code = "界" * 80 + "\n\x1b[2J" + "e\u0301" * 40
    rendered = render_human(
        DiagnosticEvent(
            "extension.event",
            "concise actionable headline",
            severity=severity,
            code=code,
        ),
        color=color,
        width=width,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    lines = plain.splitlines()

    assert all(_display_width(line) <= width for line in lines)
    assert lines[-1] == "  concise actionable headline"
    assert max(len(line) - len(line.lstrip()) for line in lines) <= 2
    assert r"\n\x1b[2J" in plain
    assert "\x1b[2J" not in rendered
    assert "\x1b" not in plain


@pytest.mark.parametrize(
    ("severity", "code", "expected"),
    [
        (
            DiagnosticSeverity.ERROR,
            "E1234",
            "protodial: (E1234) concise headline",
        ),
        (
            DiagnosticSeverity.WARNING,
            "W1234",
            "* warning (W1234): concise headline",
        ),
    ],
)
def test_standard_diagnostic_code_headline_layout_is_unchanged(
    severity: DiagnosticSeverity, code: str, expected: str
) -> None:
    rendered = render_human(
        DiagnosticEvent(
            "extension.event",
            "concise headline",
            severity=severity,
            code=code,
        ),
        width=60,
    )

    assert rendered == expected


def test_human_semantic_visibility_filters_only_empty_values() -> None:
    details = (
        {
            "type": "type.googleapis.com/google.rpc.BadRequest",
            "field_violations": (
                {"field": "  ", "description": "\t"},
                {"field": "resource", "description": "   "},
                {"field": "\t", "description": "must not be empty"},
            ),
        },
        {
            "type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": " ",
            "domain": "\t",
            "metadata": {" ": "visible", "empty": "\n"},
        },
        {
            "type": "type.googleapis.com/google.rpc.Help",
            "links": (
                {"description": "  ", "url": "https://example.test/docs"},
                {"description": "read the guide", "url": "\t"},
                {"description": " ", "url": ""},
            ),
        },
        {
            "type": "type.googleapis.com/google.rpc.LocalizedMessage",
            "locale": "  ",
            "message": "visible localized message",
        },
        {
            "type": "type.googleapis.com/google.rpc.DebugInfo",
            "detail": "\t",
            "stack_entries": ("  ",),
        },
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        metadata=(("blank", " \t"), (" ", "visible")),
        causes=(" \t",),
        help=("\n",),
        notes=("  ",),
        details=details,
    )

    human = render_human(event)
    machine_stream = io.StringIO()
    StderrDiagnosticSink(
        0, stream=machine_stream, diagnostic_format="json"
    ).emit(event)
    machine = json.loads(machine_stream.getvalue())

    assert "request: invalid" in human
    assert "resource: invalid" in human
    assert "request: must not be empty" in human
    assert "error information:" not in human
    assert "debug information:" not in human
    assert "https://example.test/docs" in human
    assert "read the guide" in human
    assert "localized message:" in human
    assert "localized message (" not in human
    assert "  reason:" not in human
    assert "  note:" not in human
    assert "* metadata" not in human
    assert machine["metadata"][0]["value"] == " \t"
    assert machine["causes"] == [" \t"]
    assert machine["details"][0]["field_violations"][0]["field"] == "  "


def test_custom_short_name_lookalikes_are_unknown_and_lossless() -> None:
    custom_names = (
        "BadRequest",
        "DebugInfo",
        "ErrorInfo",
        "Help",
        "LocalizedMessage",
        "PreconditionFailure",
        "QuotaFailure",
        "RequestInfo",
        "ResourceInfo",
        "RetryInfo",
    )
    custom_details = tuple(
        {
            "type": f"type.googleapis.com/acme.{name}",
            "custom_value": f"MUST_SURVIVE_{name}",
        }
        for name in custom_names
    )
    event = DiagnosticEvent(
        "rpc.failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        details=(
            *custom_details,
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "STANDARD_DEBUG",
            },
        ),
    )

    rendered = render_human(event)

    assert rendered.index("debug information:") < rendered.index(
        "type.googleapis.com/acme.BadRequest:"
    )
    for name in custom_names:
        assert f"type.googleapis.com/acme.{name}:" in rendered
        assert f"custom_value: MUST_SURVIVE_{name}" in rendered
    assert "STANDARD_DEBUG" in rendered


def test_unknown_detail_renders_every_payload_sibling_once_in_input_order() -> (
    None
):
    event = DiagnosticEvent(
        "extension.failed",
        level=0,
        details=(
            {
                "type": "type.example/Everything",
                "title": "custom detail",
                "items": {"z": 1, "a": "two"},
                "value": b"opaque",
                "custom_value": "MUST_SURVIVE",
                "warning": "decode warning",
            },
        ),
    )

    rendered = render_human(event, width=32)

    assert rendered.count("custom detail:") == 1
    assert rendered.count("type.example/Everything") == 1
    assert rendered.count("items:") == 1
    assert rendered.count("value:") == 2  # value + custom_value
    assert rendered.count("decode warning") == 1
    assert "base64:b3BhcXVl" in rendered
    assert "MUST_SURVIVE" in rendered
    assert rendered.index("items:") < rendered.index("  value:")
    assert rendered.index("  value:") < rendered.index("custom_value:")
    assert all(_display_width(line) <= 32 for line in rendered.splitlines())


@pytest.mark.parametrize("color", [False, True])
def test_generic_truncation_preserves_span_action_and_later_help(
    color: bool,
) -> None:
    event = DiagnosticEvent(
        "command.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
        spans=(
            DiagnosticSpan(
                "request.json",
                text="x" + "\u0301" * 70_000,
                start=0,
                end=1,
                label="MUST_SURVIVE_ACTION",
            ),
        ),
        help=("FIX_NOW",),
    )
    complete = render_human(event, color=color)
    rendered = truncate_generic_human(
        event,
        complete,
        64 * 1024 - 2,
        color=color,
        width=100,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert "1 | …" in plain
    assert "^ MUST_SURVIVE_ACTION" in plain
    assert "help: FIX_NOW" in plain
    assert "diagnostic truncated" in plain
    assert len(rendered.encode("utf-8")) <= 64 * 1024 - 2
    assert "\x1b" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered)


def test_generic_truncation_counts_omissions_and_keeps_help() -> None:
    event = DiagnosticEvent(
        "command.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
        causes=("x" * 70_000,),
        help=("FIX_NOW",),
    )
    complete = render_human(event)
    rendered = truncate_generic_human(
        event,
        complete,
        64 * 1024 - 2,
        color=False,
        width=100,
    )
    marker = "\n  note: diagnostic truncated; "
    body, count = rendered.split(marker, 1)

    assert "help: FIX_NOW" in body
    assert int(count.removesuffix(" characters omitted")) == (
        len(complete) - len(body)
    )


def test_generic_truncation_preserves_block_order_past_old_numeric_ranges() -> (
    None
):
    event = DiagnosticEvent(
        "extension.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E9999",
        spans=tuple(
            DiagnosticSpan(
                f"source_{index:03d}",
                text="bad",
                start=0,
                end=3,
                label=f"SPAN_{index:03d}",
            )
            for index in range(101)
        ),
        causes=tuple(f"CAUSE_{index:03d}" for index in range(101)),
        help=tuple(f"HELP_{index:03d}" for index in range(101)),
        notes=tuple(f"NOTE_{index:03d}" for index in range(101)),
        metadata=tuple(
            (f"meta_{index:03d}", f"value_{index:03d}") for index in range(101)
        ),
        details=(
            *(
                {
                    "type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": f"DETAIL_{index:03d}",
                }
                for index in range(101)
            ),
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "x" * 70_000,
            },
        ),
    )
    complete = render_human(event, width=100)
    rendered = truncate_generic_human(
        event, complete, 64 * 1024 - 2, color=False, width=100
    )

    assert "diagnostic truncated" in rendered
    assert rendered.index("SPAN_100") < rendered.index("reason: CAUSE_000")
    assert rendered.index("reason: CAUSE_100") < rendered.index(
        "help: HELP_000"
    )
    assert rendered.index("help: HELP_100") < rendered.index("note: NOTE_000")
    assert rendered.index("note: NOTE_100") < rendered.index("* metadata")
    assert rendered.index("meta_100") < rendered.index("DETAIL_000")
    assert rendered.index("DETAIL_000") < rendered.index("DETAIL_100")


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


@pytest.mark.parametrize("width", [60, 61, 79, 120])
@pytest.mark.parametrize("color", [False, True])
def test_rpc_truncation_tiny_limits_remain_utf8_ansi_and_width_safe(
    width: int, color: bool
) -> None:
    event = DiagnosticEvent(
        "rpc.failed",
        "界👩‍💻" * 4_000,
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={"grpc_status": {"name": "INVALID_ARGUMENT", "number": 3}},
        details=(
            {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {"field": "resource", "description": "x" * 70_000},
                ),
            },
        ),
    )

    for limit in (1, 2, 7, 31, 63, 127, 511, 4096):
        rendered = truncate_rpc_failure_human(
            event,
            limit,
            color=color,
            width=width,
        )
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

        assert len(rendered.encode("utf-8")) <= limit
        assert rendered.encode("utf-8").decode("utf-8") == rendered
        assert not rendered.endswith("\x1b")
        assert not re.search(r"\x1b\[[0-9;]*$", rendered)
        assert "\x1b" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        assert all(_display_width(line) <= width for line in plain.splitlines())


@pytest.mark.parametrize("color", [False, True])
def test_generic_truncation_tiny_limits_are_utf8_grapheme_and_ansi_safe(
    color: bool,
) -> None:
    glyph = "\U0001f469\u200d\U0001f4bb"
    event = DiagnosticEvent(
        "command.failed",
        "bad " + glyph * 30_000,
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E2001",
        help=("FIX_" + glyph,),
    )
    complete = render_human(event, color=color)

    for limit in (1, 2, 7, 31, 63, 127, 511, 4096):
        rendered = truncate_generic_human(
            event,
            complete,
            limit,
            color=color,
            width=60,
        )
        assert len(rendered.encode("utf-8")) <= limit
        assert rendered.encode("utf-8").decode("utf-8") == rendered
        assert not rendered.endswith("\x1b")
        assert not re.search(r"\x1b\[[0-9;]*$", rendered)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        marker = "\n  note: diagnostic truncated; "
        if not color and marker in rendered:
            body, count = rendered.split(marker, 1)
            assert int(count.removesuffix(" characters omitted")) == (
                len(complete) - len(body)
            )
        if glyph in plain:
            assert all(
                cluster != "\U0001f469" for cluster in grapheme_clusters(plain)
            )


def test_bounded_generic_base_reads_only_source_order_context_candidates() -> (
    None
):
    fields = {f"field_{index:04d}": index for index in range(5_000)}
    event = DiagnosticEvent(
        "extension.failed",
        "real message",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E9999",
        fields=fields,
    )
    metrics = JsonTruncationMetrics()

    bounded = _bounded_diagnostic_json_base(
        event, 7, 16 * 1024, _metrics=metrics
    )

    assert bounded["event"] == "extension.failed"
    assert bounded["code"] == "E9999"
    assert bounded["message"] == "real message"
    assert bounded["notes"] == [
        "diagnostic truncated to fit the 64 KiB sink limit"
    ]
    assert list(bounded["context"]) == list(fields)[:8]
    assert metrics.base_candidates == 1
    assert metrics.context_entries == 8


def test_bounded_json_detail_selection_is_linear_for_5k_details() -> None:
    details = tuple(
        (
            {
                "type": "type.googleapis.com/google.rpc.DebugInfo",
                "detail": "debug-" + "x" * 64,
                "wire_index": index,
            }
            if index % 2 == 0
            else {
                "type": "type.googleapis.com/google.rpc.BadRequest",
                "field_violations": (
                    {
                        "field": f"field_{index}",
                        "description": "fix this value",
                    },
                ),
                "wire_index": index,
            }
        )
        for index in range(5_000)
    )
    event = DiagnosticEvent(
        "extension.failed",
        "failed",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        details=details,
    )
    metrics = JsonTruncationMetrics()

    byte_limit = 64 * 1024 - 2
    rendered = _truncate_json_with_bounded_base(
        event, 1, byte_limit, _metrics=metrics
    )
    value = json.loads(rendered)
    marker = value["details"][-1]
    survivors = [item["wire_index"] for item in value["details"][:-1]]
    omitted = marker["omitted_detail_indexes"]

    assert len(rendered.encode("utf-8")) <= byte_limit
    assert survivors == sorted(survivors)
    assert survivors and all(index % 2 == 1 for index in survivors)
    assert sorted([*survivors, *omitted]) == list(range(5_000))
    assert marker["original_detail_count"] == 5_000
    remaining = json.dumps(
        [details[index] for index in omitted],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert marker["preview"] == remaining[: len(marker["preview"])]
    assert marker["characters_omitted"] == (
        len(remaining) - len(marker["preview"])
    )
    assert metrics.normalized_details == 5_000
    assert metrics.serialized_details == 5_000
    assert metrics.selection_candidates == 5_000
    assert metrics.final_detail_scans == 5_000
    assert metrics.preview_candidates <= len(remaining).bit_length() + 1


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
