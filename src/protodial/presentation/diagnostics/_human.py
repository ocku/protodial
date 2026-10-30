"""Compose human diagnostics and RPC failure presentations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._detail_values import (
    detail_warning_text,
    has_visible_detail_value,
    prioritized_rich_details,
)
from ._details import (
    human_detail,
)
from ._human_layout import (
    annotation_lines,
    headline_lines,
    metadata_lines,
    rpc_annotation_lines,
)
from ._messages import (
    event_message,
)
from ._spans import (
    render_span,
)
from ._terminal import (
    abbreviate_cells,
    detail_identity,
)
from ._text import (
    count,
    counter,
    duration_suffix,
    safe,
    status_parts,
    style_semantic_text,
)
from .models import (
    DiagnosticEvent,
    DiagnosticValue,
)


def _rpc_failure_status(event: DiagnosticEvent, width: int) -> str:
    if "grpc_status" in event.fields or "status" in event.fields:
        name, number = status_parts(event)
        safe_name = abbreviate_cells(
            safe(name),
            max(1, width * 3),
            max_bytes=1024,
            max_codepoints=512,
        )
        status_text = (
            f"{safe_name} ({number})" if number is not None else safe_name
        )
    else:
        message = event_message(event)
        before, separator, after = message.partition(" failed with ")
        status_text = (
            after if separator and before.startswith("RPC") else message
        )

    timing = duration_suffix(event.fields.get("elapsed_seconds"))
    counters: list[str] = []
    sent = counter(event.fields, "sent", "sent_messages")
    if sent is not None:
        counters.append(f"sent {count(sent)}")
    received = counter(event.fields, "received", "received_messages")
    if received is not None:
        counters.append(f"received {count(received)}")
    count_suffix = f"; {'; '.join(counters)}" if counters else ""
    return f"{status_text}{timing}{count_suffix}"


def _rpc_method_context(event: DiagnosticEvent, width: int) -> str | None:
    method = event.fields.get("method")
    return (
        f"`{detail_identity(method, width)}`"
        if isinstance(method, str) and method
        else None
    )


def _rpc_failure_detail_group(
    detail: DiagnosticValue,
    *,
    field_prefix: str | None,
    color: bool,
    width: int,
) -> list[str]:
    """Render one detail as an indivisible truncation group."""
    detail_lines = human_detail(
        detail,
        color=color,
        field_prefix=field_prefix,
        width=width,
    )
    if isinstance(detail, Mapping) and has_visible_detail_value(
        detail.get("warning")
    ):
        detail_lines.extend(
            rpc_annotation_lines(
                "note",
                detail_warning_text(detail, width),
                role="strong",
                color=color,
                width=width,
            )
        )
    return [style_semantic_text(line, color) for line in detail_lines]


def _rpc_failure_details(
    event: DiagnosticEvent,
    *,
    field_prefix: str | None,
    color: bool,
    width: int,
) -> list[str]:
    lines: list[str] = []
    for _, detail in prioritized_rich_details(event.details):
        lines.extend(
            _rpc_failure_detail_group(
                detail,
                field_prefix=field_prefix,
                color=color,
                width=width,
            )
        )
    return lines


def _rpc_failure_annotations(
    label: str,
    values: Sequence[str],
    *,
    role: str,
    color: bool,
    width: int,
) -> list[str]:
    lines: list[str] = []
    for value in values:
        lines.extend(
            rpc_annotation_lines(
                label,
                value,
                role=role,
                color=color,
                width=width,
            )
        )
    return lines


def render_rpc_failure(
    event: DiagnosticEvent, *, color: bool, width: int
) -> str:
    """Render RPC failures in order of diagnostic value to the caller."""
    lines: list[str] = []
    invocation = _rpc_method_context(event, width)
    request_type = event.fields.get("request_type")
    field_prefix = (
        detail_identity(request_type.rsplit(".", 1)[-1], width)
        if isinstance(request_type, str)
        else None
    )

    lines.extend(
        _rpc_failure_details(
            event,
            field_prefix=field_prefix,
            color=color,
            width=width,
        )
    )
    lines.extend(
        headline_lines(
            event, _rpc_failure_status(event, width), color=color, width=width
        )
    )
    lines.extend(
        _rpc_failure_annotations(
            "reason",
            event.causes,
            role="strong",
            color=color,
            width=width,
        )
    )
    if invocation is not None:
        lines.extend(
            rpc_annotation_lines(
                "call",
                invocation,
                role="strong",
                color=color,
                width=width,
                value_role="location",
            )
        )
    lines.extend(
        _rpc_failure_annotations(
            "help",
            event.help,
            role="help",
            color=color,
            width=width,
        )
    )
    lines.extend(
        _rpc_failure_annotations(
            "note",
            event.notes,
            role="strong",
            color=color,
            width=width,
        )
    )
    if event.metadata:
        lines.extend(metadata_lines(event, color=color, width=width))
    return "\n".join(lines)


def render_human(
    event: DiagnosticEvent, *, color: bool = False, width: int = 100
) -> str:
    """Render one event using the human grammar."""
    if event.name in {"rpc.headers", "rpc.trailers"}:
        return "\n".join(metadata_lines(event, color=color, width=width))
    if event.name == "rpc.failed":
        return render_rpc_failure(event, color=color, width=width)
    severity = event.severity.value
    preserve_schema_lines = (
        event.name == "input.invalid"
        and event.fields.get("kind") == "protojson_schema"
    )
    lines = headline_lines(
        event,
        event_message(event),
        color=color,
        width=width,
        wrap=not preserve_schema_lines,
    )
    for span in event.spans:
        lines.extend(
            render_span(span, color=color, severity=severity, width=width)
        )
    for cause in event.causes:
        lines.extend(
            annotation_lines(
                "reason",
                cause,
                role="strong",
                color=color,
                width=width,
                wrap=not preserve_schema_lines,
            )
        )
    for help_item in event.help:
        lines.extend(
            annotation_lines(
                "help",
                help_item,
                role="help",
                color=color,
                width=width,
            )
        )
    for note in event.notes:
        lines.extend(
            annotation_lines(
                "note",
                note,
                role="strong",
                color=color,
                width=width,
            )
        )
    metadata = (
        metadata_lines(event, color=color, width=width)
        if event.metadata
        else []
    )
    if metadata:
        if event.name != "rpc.started":
            lines.append("")
        lines.extend(metadata)
    for _, detail in prioritized_rich_details(event.details):
        detail_lines = human_detail(detail, color=color, width=width)
        if isinstance(detail, Mapping) and has_visible_detail_value(
            detail.get("warning")
        ):
            detail_lines.extend(
                annotation_lines(
                    "note",
                    detail_warning_text(detail, width),
                    role="strong",
                    color=color,
                    width=width,
                )
            )
        if detail_lines:
            lines.extend(
                (
                    "",
                    *(
                        style_semantic_text(line, color)
                        for line in detail_lines
                    ),
                )
            )
    return "\n".join(lines)
