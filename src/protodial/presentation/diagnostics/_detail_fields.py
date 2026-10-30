"""Render rich-status details containing named scalar fields."""

from __future__ import annotations

from collections.abc import Mapping

from ._detail_values import (
    detail_value_lines,
    has_visible_detail_value,
    render_value,
)
from ._terminal import (
    detail_identity,
    wrap_cells,
)
from ._text import (
    format_duration,
    safe,
    style,
)
from .models import (
    DiagnosticValue,
)


def render_error_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render ErrorInfo reason, domain, and metadata."""
    content: list[str] = []
    reason = detail.get("reason")
    domain = detail.get("domain")
    has_reason = has_visible_detail_value(reason)
    has_domain = has_visible_detail_value(domain)
    if has_reason and has_domain:
        content.extend(
            detail_value_lines(
                "  ",
                f"`{detail_identity(reason, width)}` from "
                f"`{detail_identity(domain, width)}`",
                width=width,
            )
        )
    elif has_reason:
        content.extend(
            detail_value_lines(
                "  reason ",
                f"`{detail_identity(reason, width)}`",
                width=width,
            )
        )
    elif has_domain:
        content.extend(
            detail_value_lines(
                "  domain ",
                f"`{detail_identity(domain, width)}`",
                width=width,
            )
        )
    metadata = detail.get("metadata")
    visible_metadata = (
        [
            (key, value)
            for key, value in metadata.items()
            if has_visible_detail_value(key) and has_visible_detail_value(value)
        ]
        if isinstance(metadata, Mapping)
        else []
    )
    if visible_metadata:
        content.append("  metadata:")
        for key, value in visible_metadata:
            safe_key = detail_identity(key, width)
            content.extend(
                detail_value_lines(
                    f"    {safe_key}: ",
                    render_value(value),
                    width=width,
                )
            )
    if not content:
        return []
    return [style("error information:", "strong", color), *content]


def render_retry_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str] | None:
    """Render RetryInfo using a compact duration."""
    if "retry_delay" not in detail:
        return None
    delay = detail["retry_delay"]
    if not has_visible_detail_value(delay):
        return []
    rendered_delay = safe(delay)
    if isinstance(delay, str) and delay.endswith("s"):
        try:
            rendered_delay = format_duration(float(delay[:-1]))
        except (OverflowError, ValueError):
            pass
    return detail_value_lines("  retry after ", rendered_delay, width=width)


def render_resource_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render ResourceInfo identity, owner, and description."""
    lines: list[str] = []
    resource_type = detail.get("resource_type")
    resource_name = detail.get("resource_name")
    has_type = has_visible_detail_value(resource_type)
    has_name = has_visible_detail_value(resource_name)
    if has_type and has_name:
        lines.extend(
            detail_value_lines(
                "  ",
                f"`{detail_identity(resource_type, width)}` resource "
                f"`{detail_identity(resource_name, width)}`",
                width=width,
            )
        )
    elif has_name:
        lines.extend(
            detail_value_lines(
                "  resource ",
                f"`{detail_identity(resource_name, width)}`",
                width=width,
            )
        )
    elif has_type:
        lines.extend(
            detail_value_lines(
                "  resource type ",
                f"`{detail_identity(resource_type, width)}`",
                width=width,
            )
        )
    if has_visible_detail_value(detail.get("owner")):
        lines.extend(
            detail_value_lines(
                "  owned by ",
                f"`{detail_identity(detail['owner'], width)}`",
                width=width,
            )
        )
    if has_visible_detail_value(detail.get("description")):
        lines.extend(
            detail_value_lines("  ", detail["description"], width=width)
        )
    if not lines:
        return []
    return [style("resource information:", "strong", color), *lines]


def render_request_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render RequestInfo request and serving identifiers."""
    lines: list[str] = []
    request_id = detail.get("request_id")
    serving_data = detail.get("serving_data")
    has_request_id = has_visible_detail_value(request_id)
    has_serving_data = has_visible_detail_value(serving_data)
    if has_request_id and has_serving_data:
        lines.extend(
            detail_value_lines(
                "  request ",
                f"`{detail_identity(request_id, width)}` served by "
                f"`{detail_identity(serving_data, width)}`",
                width=width,
            )
        )
    elif has_request_id:
        lines.extend(
            detail_value_lines(
                "  request ",
                f"`{detail_identity(request_id, width)}`",
                width=width,
            )
        )
    elif has_serving_data:
        lines.extend(
            detail_value_lines(
                "  served by ",
                f"`{detail_identity(serving_data, width)}`",
                width=width,
            )
        )
    if not lines:
        return []
    return [style("request information:", "strong", color), *lines]


def render_localized_message(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render a LocalizedMessage with its locale."""
    locale = detail.get("locale")
    message = detail.get("message", "")
    if not has_visible_detail_value(message):
        return []
    heading = "localized message"
    if has_visible_detail_value(locale):
        heading += f" ({detail_identity(locale, width)})"
    heading_lines = wrap_cells(heading + ":", max(1, width))
    return [
        *(style(line, "strong", color) for line in heading_lines),
        *detail_value_lines("  ", message, width=width),
    ]
