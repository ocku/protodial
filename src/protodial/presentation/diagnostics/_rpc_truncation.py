"""Select useful sections of oversized RPC failure diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from ._detail_values import (
    prioritized_rich_details,
    rich_detail_priority,
)
from ._details import (
    rich_detail_truncation_items,
)
from ._human import (
    render_rpc_failure,
)
from ._human_truncation import (
    HumanTruncationGroup,
    assemble_selected_human_groups,
    human_truncation_notice,
    prepare_human_groups,
    truncate_human,
)
from ._render_policy import (
    MAX_RICH_DETAIL_PREVIEW_CHARS,
    HumanTruncationMetrics,
)
from ._terminal import (
    abbreviate_prefix,
    detail_identity,
)
from .models import (
    DiagnosticEvent,
)


def truncate_rpc_failure_human(
    event: DiagnosticEvent,
    limit: int,
    *,
    color: bool,
    width: int,
    _metrics: HumanTruncationMetrics | None = None,
) -> str:
    """Bound a failure while retaining its status, reason, and call context.

    Rich details lead the normal layout because they are often the most
    actionable content.  Under an output cap, however, they must not consume
    the status block that explains which call failed.  Reserve that compact
    block first, then use the remaining budget for whole wrapped detail lines.
    """

    def abbreviated(value: str, maximum: int = 1024) -> str:
        return abbreviate_prefix(
            value,
            maximum,
            max_bytes=maximum * 4,
            max_codepoints=maximum,
        )

    essential_fields = dict(event.fields)
    for name in ("method", "status"):
        value = essential_fields.get(name)
        if isinstance(value, str):
            essential_fields[name] = abbreviated(value)
    grpc_status = essential_fields.get("grpc_status")
    if isinstance(grpc_status, Mapping):
        bounded_status = dict(grpc_status)
        status_name = bounded_status.get("name")
        if isinstance(status_name, str):
            bounded_status["name"] = abbreviated(status_name)
        essential_fields["grpc_status"] = bounded_status
    # Server prose and the method identifier are untrusted and may be large.
    # Keep a useful sample of each essential section so one pathological value
    # cannot displace the other sections.
    essential_event = replace(
        event,
        message=abbreviated(event.message),
        fields=essential_fields,
        causes=tuple(abbreviated(value) for value in event.causes[:4]),
        help=tuple(abbreviated(value) for value in event.help[:4]),
        notes=tuple(abbreviated(value) for value in event.notes[:4]),
        metadata=(),
        details=(),
    )
    essential = render_rpc_failure(essential_event, color=color, width=width)
    request_type = event.fields.get("request_type")
    field_prefix = (
        detail_identity(request_type.rsplit(".", 1)[-1], width)
        if isinstance(request_type, str)
        else None
    )
    groups: list[HumanTruncationGroup] = []
    order = 0
    for section, (_, detail) in enumerate(
        prioritized_rich_details(event.details), start=1
    ):
        items = rich_detail_truncation_items(
            detail,
            field_prefix=field_prefix,
            color=color,
            width=width,
        )
        shared_heading = next(
            (item.heading for item in items if item.heading), ""
        )
        for item in items:
            groups.append(
                HumanTruncationGroup(
                    priority=rich_detail_priority(detail),
                    order=order,
                    text=item.text,
                    section=section,
                    section_heading=shared_heading,
                )
            )
            order += 1

    prepared = prepare_human_groups(groups, metrics=_metrics)
    full_chars = 0
    full_sections: set[int] = set()
    for index, item in enumerate(prepared):
        group = item.group
        if index:
            full_chars += 1
        if group.section is not None and group.section not in full_sections:
            full_sections.add(group.section)
            full_chars += item.scaffold_chars
        full_chars += item.text_chars

    essential_bytes = len(essential.encode("utf-8"))
    retained = [False] * len(prepared)
    partial_line_counts = [0] * len(prepared)
    active_sections: set[int] = set()
    selected_group_count = 0
    preview_chars = 0
    preview_bytes = 0

    def marker_for(candidate_chars: int) -> str:
        omitted = full_chars - candidate_chars
        if omitted:
            message = (
                f"rich status truncated; {omitted} "
                "rich-detail characters omitted"
            )
        else:
            message = "diagnostic truncated to fit the 64 KiB sink limit"
        return human_truncation_notice(
            message,
            color=color,
            width=width,
        )

    def fits(candidate_chars: int, candidate_bytes: int) -> bool:
        if candidate_chars > MAX_RICH_DETAIL_PREVIEW_CHARS:
            return False
        marker = marker_for(candidate_chars)
        total_bytes = (
            candidate_bytes
            + (1 if candidate_chars else 0)
            + essential_bytes
            + 1
            + len(marker.encode("utf-8"))
        )
        return total_bytes <= limit

    # Consider every semantic item as a whole before spending spare space on
    # previews. Sizes and section scaffolding are accounted incrementally, so
    # a huge first item cannot hide a later concise correction and selection
    # remains linear in the number of items.
    for index, item in enumerate(prepared):
        if _metrics is not None:
            _metrics.selection_candidates += 1
        group = item.group
        add_scaffold = (
            item.scaffold
            if group.section is not None
            and group.section not in active_sections
            else ""
        )
        added_chars = (
            (1 if selected_group_count else 0)
            + (item.scaffold_chars if add_scaffold else 0)
            + item.text_chars
        )
        added_bytes = (
            (1 if selected_group_count else 0)
            + (item.scaffold_bytes if add_scaffold else 0)
            + item.text_bytes
        )
        if fits(preview_chars + added_chars, preview_bytes + added_bytes):
            retained[index] = True
            selected_group_count += 1
            preview_chars += added_chars
            preview_bytes += added_bytes
            if group.section is not None:
                active_sections.add(group.section)

    # Use remaining space for complete physical lines from rejected items.
    # At most one non-fitting line is tried per rejected group; accepted lines
    # consume the finite preview budget, giving a deterministic linear bound.
    for index, item in enumerate(prepared):
        if retained[index]:
            continue
        group = item.group
        for line_index, _line in enumerate(item.lines):
            if _metrics is not None:
                _metrics.partial_line_candidates += 1
            first_line = line_index == 0
            add_scaffold = (
                item.scaffold
                if first_line
                and group.section is not None
                and group.section not in active_sections
                else ""
            )
            separator = 1 if first_line and selected_group_count else 0
            internal_newline = 1 if not first_line else 0
            added_chars = (
                separator
                + internal_newline
                + (item.scaffold_chars if add_scaffold else 0)
                + item.line_chars[line_index]
            )
            added_bytes = (
                separator
                + internal_newline
                + (item.scaffold_bytes if add_scaffold else 0)
                + item.line_bytes[line_index]
            )
            if not fits(
                preview_chars + added_chars, preview_bytes + added_bytes
            ):
                break
            partial_line_counts[index] += 1
            preview_chars += added_chars
            preview_bytes += added_bytes
            if first_line:
                selected_group_count += 1
                if group.section is not None:
                    active_sections.add(group.section)

    preview = assemble_selected_human_groups(
        prepared,
        retained,
        partial_line_counts,
        metrics=_metrics,
    )
    marker = marker_for(len(preview))
    rendered = "\n".join(part for part in (preview, essential, marker) if part)
    if len(rendered.encode("utf-8")) <= limit:
        return rendered

    # Extremely small caller-selected limits cannot hold all essentials.
    # Since the essential block starts with the status, the generic bounded
    # fallback still retains the highest-priority fragment.
    subject = "rich status" if full_chars else "diagnostic"
    return truncate_human(
        essential,
        limit,
        color=color,
        subject=subject,
        width=width,
    )
