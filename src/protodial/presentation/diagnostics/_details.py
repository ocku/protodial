"""Dispatch rich-detail renderers and prepare selectable detail items."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ._detail_fields import (
    render_error_info,
    render_localized_message,
    render_request_info,
    render_resource_info,
    render_retry_info,
)
from ._detail_lists import (
    render_bad_request,
    render_debug_info,
    render_help,
    render_precondition_failure,
    render_quota_failure,
)
from ._detail_values import (
    detail_name,
    detail_value_lines,
    detail_warning_text,
    has_visible_detail_value,
    render_value,
)
from ._human_layout import (
    rpc_annotation_lines,
)
from ._render_policy import (
    DetailTruncationItem,
)
from ._terminal import (
    abbreviate_cells,
    detail_identity,
    wrap_cells,
)
from ._text import (
    safe,
    style,
    style_semantic_text,
)
from .models import (
    DiagnosticValue,
)


def _render_unknown_detail(
    detail: Mapping[str, DiagnosticValue],
    color: bool,
    width: int,
) -> list[str]:
    """Render an unknown protobuf detail without discarding its fields."""
    type_name = abbreviate_cells(
        safe(detail.get("type", "detail")),
        max(1, width * 4),
        max_bytes=1024,
        max_codepoints=512,
    )
    raw_title = detail.get("title")
    title = (
        detail_identity(raw_title, width)
        if has_visible_detail_value(raw_title)
        else type_name
    )
    title_lines = wrap_cells(title + ":", max(1, width))
    lines = [style(line, "strong", color) for line in title_lines]
    # A custom title must not replace the unknown protobuf identity.  Keep the
    # familiar title-less ``type.example/Message:`` shape, but put a distinct
    # identity directly beneath a custom heading as compact context.
    if title != type_name:
        lines.extend(detail_value_lines("  type: ", type_name, width=width))
    # ``warning`` is presented once by the caller as a note. Every other
    # sibling is payload, including simultaneous ``items`` and ``value``
    # fields used by custom Any decoders. Preserve the mapping's input order
    # for stable wire-to-human comparison.
    for key, value in detail.items():
        if key in {"title", "type", "warning"}:
            continue
        safe_key = detail_identity(key, width)
        lines.extend(
            detail_value_lines(
                f"  {safe_key}: ",
                render_value(value),
                width=width,
            )
        )
    return lines


_DETAIL_RENDERERS: Mapping[
    str,
    Callable[
        [Mapping[str, DiagnosticValue], bool, str | None, int],
        list[str] | None,
    ],
] = {
    "BadRequest": render_bad_request,
    "ErrorInfo": render_error_info,
    "RetryInfo": render_retry_info,
    "PreconditionFailure": render_precondition_failure,
    "QuotaFailure": render_quota_failure,
    "ResourceInfo": render_resource_info,
    "RequestInfo": render_request_info,
    "DebugInfo": render_debug_info,
    "Help": render_help,
    "LocalizedMessage": render_localized_message,
}


def human_detail(
    detail: DiagnosticValue,
    *,
    color: bool,
    field_prefix: str | None = None,
    width: int = 100,
) -> list[str]:
    """Render one rich-status detail with a type-specific renderer."""
    if not isinstance(detail, Mapping):
        return detail_value_lines("  ", render_value(detail), width=width)
    renderer = _DETAIL_RENDERERS.get(detail_name(detail))
    if renderer is not None:
        rendered = renderer(detail, color, field_prefix, width)
        # Known semantic details are either rendered semantically or omitted;
        # falling through would expose their empty protobuf representation.
        return rendered or []
    return _render_unknown_detail(detail, color, width)


def rich_detail_truncation_items(
    detail: DiagnosticValue,
    *,
    field_prefix: str | None,
    color: bool,
    width: int,
) -> list[DetailTruncationItem]:
    """Render independently selectable semantic items from one detail.

    Ordinary rendering deliberately treats a protobuf detail as one section.
    A bounded rendering needs a finer unit: an enormous first list item must
    not hide a later concise correction from the same detail.  Section
    headings remain shared presentation scaffolding and are emitted once when
    at least one of their items survives.
    """

    def styled(lines: Sequence[str]) -> list[str]:
        return [style_semantic_text(line, color) for line in lines]

    def item_from_lines(
        lines: Sequence[str], *, heading: bool
    ) -> DetailTruncationItem | None:
        rendered = styled(lines)
        if not rendered:
            return None
        if heading:
            if len(rendered) == 1:
                return None
            return DetailTruncationItem(
                "\n".join(rendered[1:]), heading=rendered[0]
            )
        return DetailTruncationItem("\n".join(rendered))

    items: list[DetailTruncationItem] = []
    semantic_name = detail_name(detail) if isinstance(detail, Mapping) else ""

    if isinstance(detail, Mapping) and semantic_name == "BadRequest":
        violations = detail.get("field_violations", ())
        if isinstance(violations, Sequence) and not isinstance(
            violations, (str, bytes)
        ):
            for violation in violations:
                if not isinstance(violation, Mapping):
                    continue
                rendered = render_bad_request(
                    {**detail, "field_violations": (violation,)},
                    color,
                    field_prefix,
                    width,
                )
                candidate = item_from_lines(rendered or (), heading=False)
                if candidate is not None:
                    items.append(candidate)
    elif isinstance(detail, Mapping) and semantic_name in {
        "PreconditionFailure",
        "QuotaFailure",
    }:
        renderer = (
            render_precondition_failure
            if semantic_name == "PreconditionFailure"
            else render_quota_failure
        )
        violations = detail.get("violations", ())
        if isinstance(violations, Sequence):
            for violation in violations:
                if not isinstance(violation, Mapping):
                    continue
                candidate = item_from_lines(
                    renderer(
                        {**detail, "violations": (violation,)},
                        color,
                        field_prefix,
                        width,
                    ),
                    heading=True,
                )
                if candidate is not None:
                    items.append(candidate)
    elif isinstance(detail, Mapping) and semantic_name == "Help":
        links = detail.get("links", ())
        if isinstance(links, Sequence) and not isinstance(links, (str, bytes)):
            for link in links:
                if not isinstance(link, Mapping):
                    continue
                rendered = render_help(
                    {**detail, "links": (link,)},
                    color,
                    field_prefix,
                    width,
                )
                candidate = item_from_lines(rendered or (), heading=True)
                if candidate is not None:
                    items.append(candidate)
    elif isinstance(detail, Mapping) and semantic_name == "DebugInfo":
        if has_visible_detail_value(detail.get("detail")):
            candidate = item_from_lines(
                render_debug_info(
                    {**detail, "stack_entries": ()},
                    color,
                    field_prefix,
                    width,
                ),
                heading=True,
            )
            if candidate is not None:
                items.append(candidate)
        entries = detail.get("stack_entries", ())
        if isinstance(entries, Sequence) and not isinstance(entries, str):
            for entry in entries:
                if not has_visible_detail_value(entry):
                    continue
                candidate = item_from_lines(
                    render_debug_info(
                        {**detail, "detail": "", "stack_entries": (entry,)},
                        color,
                        field_prefix,
                        width,
                    ),
                    heading=True,
                )
                if candidate is not None:
                    items.append(candidate)
    else:
        candidate = item_from_lines(
            human_detail(
                detail,
                color=color,
                field_prefix=field_prefix,
                width=width,
            ),
            heading=False,
        )
        if candidate is not None:
            items.append(candidate)

    if isinstance(detail, Mapping) and has_visible_detail_value(
        detail.get("warning")
    ):
        warning = styled(
            rpc_annotation_lines(
                "note",
                detail_warning_text(detail, width),
                role="strong",
                color=color,
                width=width,
            )
        )
        if warning:
            # A decode warning is a sibling of the semantic payload, not part
            # of a list item.  It can therefore survive independently too.
            items.append(DetailTruncationItem("\n".join(warning)))

    return items
