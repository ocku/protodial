"""Render decoded rich-status values."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO, cast

from google.protobuf.message import Message

from .._rich_details import standard_detail_name
from ..formats import MessageFormat
from ..presentation._truncation import PrefixSearchMetrics, truncate_with_marker
from ..presentation.diagnostics.models import DiagnosticValue
from .decoding import code_number, format_code, status_name
from .models import RichStatus

DEFAULT_MAX_RENDERED_CHARS = 64 * 1024


def _has_semantic_content(value: object) -> bool:
    """Return whether a protobuf-JSON value carries visible information."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(_has_semantic_content(item) for item in mapping.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence = cast(Sequence[object], value)
        return any(_has_semantic_content(item) for item in sequence)
    return bool(value)


def diagnostic_details(
    details: Sequence[object],
    message_format: MessageFormat = MessageFormat.JSON,
) -> tuple[DiagnosticValue, ...]:
    """Normalize decoded status details for the shared renderers."""

    result: list[DiagnosticValue] = []
    for detail in details:
        type_url = str(getattr(detail, "type_url", ""))
        type_name = str(getattr(detail, "type_name", ""))
        semantic_name = standard_detail_name(type_url or type_name)
        message = getattr(detail, "message", None)
        warning = getattr(detail, "warning", None)
        item: dict[str, DiagnosticValue] = {
            "type": type_url or type_name or "unknown"
        }
        if message is None:
            item["value"] = bytes(getattr(detail, "payload", b""))
        elif semantic_name is not None:
            from google.protobuf import json_format

            value = json_format.MessageToDict(
                message, preserving_proto_field_name=True
            )
            violations = value.get("field_violations")
            violation_items = (
                cast(Sequence[object], violations)
                if isinstance(violations, Sequence)
                and not isinstance(violations, (str, bytes))
                else ()
            )
            has_bad_request_violation = semantic_name == "BadRequest" and any(
                isinstance(violation, Mapping) for violation in violation_items
            )
            # Empty well-known details add neither context nor remediation.
            # A BadRequest violation is useful even when the server omitted
            # both fields: the shared renderer supplies ``request: invalid``.
            # Otherwise keep an empty detail only for a useful decode warning.
            if (
                not _has_semantic_content(value)
                and not has_bad_request_violation
                and not warning
            ):
                continue
            item.update(value)
        else:
            rendered = render_message(message, message_format)
            item["value"] = (
                json.loads(rendered)
                if message_format == MessageFormat.JSON
                else rendered
            )
        if warning:
            item["warning"] = str(warning)
        result.append(item)
    return tuple(result)


def render_rich_status(
    status: RichStatus | None,
    *,
    transport_code: object | None = None,
    transport_message: str | None = None,
    message_format: MessageFormat = MessageFormat.TEXT,
    max_rendered_chars: int = DEFAULT_MAX_RENDERED_CHARS,
) -> str:
    """Render rich status, bounded by ``max_rendered_chars`` UTF-8 bytes.

    The parameter name is retained for API compatibility.  Its unit is bytes,
    matching the diagnostic sink's output guard.  The returned string does
    not include a terminating newline; callers that add one must reserve its
    byte themselves.
    """

    if max_rendered_chars <= 0:
        raise ValueError("max_rendered_chars must be greater than zero")

    from ..presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
    )
    from ..presentation.diagnostics.rendering import render_human

    outer_code = code_number(transport_code)
    display_code = "UNKNOWN"
    if transport_code is not None or transport_message is not None:
        display_code = (
            format_code(outer_code)
            if outer_code is not None
            else status_name(transport_code)
        )
    elif status is not None and status.decoded:
        display_code = format_code(status.code)

    causes: list[str] = []
    if transport_message:
        causes.append(transport_message)
    if status is not None and status.message and status.message not in causes:
        causes.append(status.message)
    details: list[DiagnosticValue] = []
    notes = list(status.warnings if status is not None else ())
    if status is not None:
        details.extend(diagnostic_details(status.details, message_format))

    event = DiagnosticEvent(
        "rpc.failed",
        f"RPC failed with {display_code}",
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        causes=tuple(causes),
        notes=tuple(notes),
        details=tuple(details),
    )
    rendered = render_human(event)
    if len(rendered.encode("utf-8")) <= max_rendered_chars:
        return rendered
    if event.details:
        from ..presentation.diagnostics.rendering import (
            truncate_rpc_failure_human,
        )

        return truncate_rpc_failure_human(
            event,
            max_rendered_chars,
            color=False,
            width=100,
        )
    return _truncate_rendered(rendered, max_rendered_chars)


def _truncate_rendered(
    rendered: str,
    limit: int,
    *,
    _metrics: PrefixSearchMetrics | None = None,
) -> str:
    """Fit output to a UTF-8 byte limit and report omitted characters."""
    return truncate_with_marker(
        rendered,
        limit,
        marker=lambda omitted: (
            f"\n  note: rich status truncated; {omitted} characters omitted"
        ),
        fallback="rich status truncated",
        metrics=_metrics,
    )


def write_rich_status(
    status: RichStatus | None,
    *,
    stream: TextIO | None = None,
    transport_code: object | None = None,
    transport_message: str | None = None,
    message_format: MessageFormat = MessageFormat.TEXT,
    max_rendered_chars: int = DEFAULT_MAX_RENDERED_CHARS,
) -> None:
    """Write a newline-terminated status within the UTF-8 byte limit.

    A one-byte limit writes only the terminating newline.  Nonpositive limits
    are rejected consistently with :func:`render_rich_status`.
    """

    if max_rendered_chars <= 0:
        raise ValueError("max_rendered_chars must be greater than zero")

    output = sys.stderr if stream is None else stream
    if max_rendered_chars == 1:
        output.write("\n")
        output.flush()
        return

    rendered = render_rich_status(
        status,
        transport_code=transport_code,
        transport_message=transport_message,
        message_format=message_format,
        max_rendered_chars=max_rendered_chars - 1,
    )
    output.write(rendered.rstrip("\n") + "\n")
    output.flush()


def render_message(message: Message, message_format: MessageFormat) -> str:
    """Render one detail message as JSON or protobuf text."""
    if message_format == MessageFormat.JSON:
        from google.protobuf import json_format

        try:
            value = json_format.MessageToDict(
                message, preserving_proto_field_name=True
            )
            return json.dumps(
                value, ensure_ascii=False, indent=2, sort_keys=True
            )
        except json_format.Error:
            # Dynamic pools may lack descriptors required for Any JSON
            # expansion.
            pass
        except TypeError as error:
            if not str(error).startswith(
                "Can not find message descriptor by type_url:"
            ):
                raise
    elif message_format != MessageFormat.TEXT:
        raise ValueError("message_format must be 'text' or 'json'")

    from google.protobuf import text_format

    rendered = text_format.MessageToString(message).rstrip()
    return rendered or "{}"
