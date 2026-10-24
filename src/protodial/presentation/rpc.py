"""Render RPC metadata, summaries, and failures."""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

from ..request_metadata.types import MetadataIterable, MetadataValue
from .sanitization import escape_control_characters


@runtime_checkable
class NamedStatus(Protocol):
    """Provide a display name for a status value."""

    name: str


class RpcSummary(Protocol):
    """Provide the outcome and diagnostics of one RPC."""

    initial_metadata: MetadataIterable
    trailing_metadata: MetadataIterable
    code: object
    details: str
    sent_messages: int
    received_messages: int
    elapsed_seconds: float


class RpcError(Protocol):
    """Provide status details for a failed RPC."""

    def code(self) -> object:
        """Return the RPC status code."""
        ...

    def details(self) -> str:
        """Return the RPC status detail text."""
        ...


def render_metadata(
    metadata: MetadataIterable, *, width: int = 100
) -> list[str]:
    """Render ordered metadata into terminal-cell-bounded physical lines.

    A continuation always begins with two spaces.  The convention stays
    readable for long keys without allowing an attacker-controlled key to
    create an equally large hanging indent.  Duplicate entries retain their
    input order, including when either entry occupies multiple lines.
    """
    # Import lazily: diagnostics uses this public renderer for its curl-like
    # blocks, while this module intentionally remains cheap to import.
    from .diagnostics.rendering import (
        abbreviate_terminal_text,
        terminal_cell_width,
        wrap_terminal_cells,
    )

    width = max(1, width)
    lines: list[str] = []
    for key, value in metadata:
        safe_key = abbreviate_terminal_text(
            escape_control_characters(key),
            max(1, width * 3),
            max_bytes=1024,
        )
        rendered = _render_metadata_value(value)
        field_prefix = f"{safe_key}: "
        prefix_width = terminal_cell_width(field_prefix)
        if prefix_width < width:
            wrapped = wrap_terminal_cells(rendered, width - prefix_width)
            first_line = field_prefix + wrapped[0]
            # ``_wrap_cells`` keeps a wide grapheme intact even when only one
            # cell remains. Put that value on a continuation instead of
            # allowing the first line to exceed its terminal budget.
            if terminal_cell_width(first_line) <= width:
                lines.append(first_line)
                lines.extend("  " + line for line in wrapped[1:])
                continue

        # If even the key cannot fit, finish its ``key:`` token first and put
        # the value on explicitly indented continuation lines.
        lines.extend(wrap_terminal_cells(f"{safe_key}:", width))
        continuation_width = max(1, width - 2)
        lines.extend(
            "  " + line
            for line in wrap_terminal_cells(rendered, continuation_width)
        )
    return lines


def render_rpc_diagnostics(
    summary: RpcSummary,
    *,
    show_headers: bool,
    show_trailers: bool,
    show_status: bool,
) -> str:
    """Render selected summary sections through the human event renderer."""
    from .diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
        DiagnosticValue,
    )
    from .diagnostics.rendering import render_human

    sections: list[str] = []
    if show_headers:
        rendered = render_human(
            DiagnosticEvent(
                "rpc.headers", metadata=tuple(summary.initial_metadata)
            )
        )
        if rendered.strip():
            sections.append(rendered)
    if show_trailers:
        rendered = render_human(
            DiagnosticEvent(
                "rpc.trailers", metadata=tuple(summary.trailing_metadata)
            )
        )
        if rendered.strip():
            sections.append(rendered)
    if show_status:
        status, number = status_parts(summary.code)
        non_ok = status != "OK"
        causes = (summary.details,) if non_ok and summary.details else ()
        fields: dict[str, DiagnosticValue] = {
            "status": status,
            "elapsed_seconds": summary.elapsed_seconds,
            "sent": summary.sent_messages,
            "received": summary.received_messages,
        }
        if non_ok:
            fields["grpc_status"] = {"name": status, "number": number}
        rendered = render_human(
            DiagnosticEvent(
                "rpc.completed",
                severity=(
                    DiagnosticSeverity.STATUS
                    if status == "OK"
                    else DiagnosticSeverity.WARNING
                ),
                code=None if not non_ok else "W4101",
                fields=fields,
                causes=causes,
            )
        )
        if rendered.strip():
            sections.append(rendered)
    return "\n".join(sections)


def render_rpc_error(error: RpcError) -> str:
    """Render a gRPC failure through the uncolored human renderer."""
    from .diagnostics.models import DiagnosticEvent, DiagnosticSeverity
    from .diagnostics.rendering import render_human

    code, number = status_parts(error.code())
    code_text = f"{code} ({number})" if number is not None else code
    details = error.details()
    return render_human(
        DiagnosticEvent(
            "rpc.failed",
            f"RPC failed with {code_text}",
            severity=DiagnosticSeverity.ERROR,
            code="E4103",
            causes=(details,) if details else (),
        )
    )


def _render_metadata_value(value: MetadataValue) -> str:
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    return escape_control_characters(value)


def _status_name(code: object) -> str:
    return code.name if isinstance(code, NamedStatus) else str(code)


def status_parts(code: object) -> tuple[str, int | None]:
    """Return a status display name and grpc numeric value when available."""
    name = _status_name(code)
    raw = getattr(code, "value", None)
    if isinstance(raw, tuple) and raw and isinstance(raw[0], int):
        return name, raw[0]
    if isinstance(raw, int) and not isinstance(raw, bool):
        return name, raw
    if isinstance(code, int) and not isinstance(code, bool):
        return name, code
    return name, None
