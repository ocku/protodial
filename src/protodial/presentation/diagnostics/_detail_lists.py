"""Render rich-status details containing violations and lists."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._detail_values import (
    detail_value_lines,
    has_visible_detail_value,
)
from ._terminal import (
    detail_identity,
)
from ._text import (
    style,
)
from .models import (
    DiagnosticValue,
)


def render_bad_request(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str] | None:
    """Render BadRequest field violations when present."""
    if "field_violations" not in detail:
        return None
    lines: list[str] = []
    violations = detail["field_violations"]
    if isinstance(violations, Sequence) and not isinstance(
        violations, (str, bytes)
    ):
        for violation in violations:
            if not isinstance(violation, Mapping):
                continue
            raw_field = violation.get("field")
            raw_description = violation.get("description")
            field = detail_identity(
                raw_field if has_visible_detail_value(raw_field) else "request",
                width,
            )
            description = (
                raw_description
                if has_visible_detail_value(raw_description)
                else "invalid"
            )
            if field_prefix and not (
                field == field_prefix or field.startswith(field_prefix + ".")
            ):
                field = f"{field_prefix}.{field}"
            prefix = f"{field}: "
            styled_prefix = f"{style(field, 'strong', color)}: "
            lines.extend(
                detail_value_lines(
                    prefix,
                    description,
                    width=width,
                    styled_prefix=styled_prefix,
                )
            )
    return lines


def _render_violation_list(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    heading: str,
    width: int,
) -> list[str]:
    """Render precondition-like typed violations."""
    lines: list[str] = []
    violations = detail.get("violations", ())
    if isinstance(violations, Sequence):
        for violation in violations:
            if not isinstance(violation, Mapping):
                continue
            subject = violation.get("subject")
            kind = violation.get("type")
            description = violation.get("description", "")
            has_subject = has_visible_detail_value(subject)
            has_kind = has_visible_detail_value(kind)
            has_description = has_visible_detail_value(description)
            if not (has_subject or has_kind or has_description):
                continue
            context = ""
            if has_kind and has_subject:
                context = (
                    f"`{detail_identity(kind, width)}` on "
                    f"`{detail_identity(subject, width)}`: "
                )
            elif has_subject:
                context = f"`{detail_identity(subject, width)}`: "
            elif has_kind:
                context = f"`{detail_identity(kind, width)}`: "
            if has_description:
                lines.extend(
                    detail_value_lines(
                        f"  - {context}", description, width=width
                    )
                )
            else:
                lines.extend(
                    detail_value_lines(
                        "  - ", context.removesuffix(": "), width=width
                    )
                )
    if not lines:
        return []
    return [style(heading, "strong", color), *lines]


def render_precondition_failure(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render PreconditionFailure violations."""
    return _render_violation_list(
        detail, color, "precondition violations:", width
    )


def render_quota_failure(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render QuotaFailure violations."""
    return _render_violation_list(detail, color, "quota violations:", width)


def render_debug_info(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str]:
    """Render DebugInfo text and stack entries."""
    lines: list[str] = []
    if has_visible_detail_value(detail.get("detail")):
        lines.extend(detail_value_lines("  ", detail["detail"], width=width))
    entries = detail.get("stack_entries")
    if isinstance(entries, Sequence) and not isinstance(entries, str):
        for entry in entries:
            if has_visible_detail_value(entry):
                lines.extend(detail_value_lines("  - ", entry, width=width))
    if not lines:
        return []
    return [style("debug information:", "strong", color), *lines]


def render_help(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    field_prefix: str | None,
    width: int,
) -> list[str] | None:
    """Render Help links when present."""
    if "links" not in detail:
        return None
    lines: list[str] = []
    links = detail["links"]
    if isinstance(links, Sequence) and not isinstance(links, (str, bytes)):
        for link in links:
            if not isinstance(link, Mapping):
                continue
            description = link.get("description")
            url = link.get("url")
            has_description = has_visible_detail_value(description)
            has_url = has_visible_detail_value(url)
            if not (has_description or has_url):
                continue
            if has_description and has_url:
                lines.extend(
                    detail_value_lines(
                        f"  {detail_identity(description, width)}: ",
                        url,
                        width=width,
                    )
                )
            else:
                visible_value = description if has_description else url
                lines.extend(
                    detail_value_lines("  ", visible_value, width=width)
                )
    if not lines:
        return []
    return [style("help:", "strong", color), *lines]
