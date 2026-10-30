"""Select useful sections of oversized non-RPC diagnostics."""

from __future__ import annotations

from .._truncation import (
    truncate_with_marker,
)
from ._detail_values import (
    prioritized_rich_details,
    rich_detail_priority,
)
from ._details import (
    rich_detail_truncation_items,
)
from ._human_layout import (
    annotation_lines,
    headline_lines,
    metadata_lines,
)
from ._human_truncation import (
    HumanTruncationGroup,
    assemble_selected_human_groups,
    bounded_human_span,
    bounded_human_text,
    prepare_human_groups,
)
from ._messages import (
    event_message,
)
from .models import (
    DiagnosticEvent,
)


def truncate_generic_human(
    event: DiagnosticEvent,
    rendered: str,
    limit: int,
    *,
    color: bool,
    width: int,
) -> str:
    """Structurally bound a non-RPC human diagnostic.

    The complete rendering remains unchanged when it fits. Under pressure,
    the headline, highlighted spans, and explicit remediation are considered
    first. Causes and corrective details follow, then supporting notes,
    metadata, and finally debugging or unknown detail payloads. Selection is
    independent of display order, so oversized early context cannot hide a
    later concise action.
    """
    if len(rendered.encode("utf-8")) <= limit:
        return rendered

    severity = event.severity.value
    preserve_schema_lines = (
        event.name == "input.invalid"
        and event.fields.get("kind") == "protojson_schema"
    )
    groups: list[HumanTruncationGroup] = []
    next_order = 0

    def add_group(
        priority: int,
        text: str,
        *,
        section: int | None = None,
        section_prefix: str = "",
        section_heading: str = "",
    ) -> None:
        nonlocal next_order
        groups.append(
            HumanTruncationGroup(
                priority,
                next_order,
                text,
                section,
                section_prefix,
                section_heading,
            )
        )
        next_order += 1

    bounded_message = bounded_human_text(event_message(event), width)
    add_group(
        0,
        "\n".join(
            headline_lines(
                event,
                bounded_message,
                color=color,
                width=width,
                wrap=not preserve_schema_lines,
            )
        ),
    )

    for span in event.spans:
        add_group(
            1,
            bounded_human_span(
                span,
                color=color,
                severity=severity,
                width=width,
            ),
        )

    for cause in event.causes:
        lines = annotation_lines(
            "reason",
            bounded_human_text(cause, width),
            role="strong",
            color=color,
            width=width,
            wrap=not preserve_schema_lines,
        )
        if lines:
            add_group(2, "\n".join(lines))

    for help_item in event.help:
        lines = annotation_lines(
            "help",
            bounded_human_text(help_item, width),
            role="help",
            color=color,
            width=width,
        )
        if lines:
            add_group(1, "\n".join(lines))

    for note in event.notes:
        lines = annotation_lines(
            "note",
            bounded_human_text(note, width),
            role="strong",
            color=color,
            width=width,
        )
        if lines:
            add_group(4, "\n".join(lines))

    if event.metadata:
        metadata = metadata_lines(event, color=color, width=width)
        if metadata:
            prefix = "" if event.name == "rpc.started" else "\n"
            add_group(5, prefix + "\n".join(metadata))

    for section, (_, detail) in enumerate(
        prioritized_rich_details(event.details), start=1
    ):
        items = rich_detail_truncation_items(
            detail,
            field_prefix=None,
            color=color,
            width=width,
        )
        if not items:
            continue
        detail_priority = rich_detail_priority(detail)
        # Corrective details compete with reasons; compact contextual details
        # precede notes, while debug and opaque bulk come last.
        priority = {0: 2, 1: 3, 2: 6}.get(detail_priority, 7)
        shared_heading = next(
            (item.heading for item in items if item.heading), ""
        )
        for item in items:
            add_group(
                priority,
                item.text,
                section=section,
                section_prefix="\n",
                section_heading=shared_heading,
            )

    reset = "\x1b[0m" if color else ""
    prepared = prepare_human_groups(groups)
    retained_flags = [False] * len(groups)
    active_sections: set[int] = set()
    # Reserve the largest possible marker once so selecting thousands of
    # independent detail groups stays linear rather than repeatedly rebuilding
    # and encoding an ever-growing candidate.
    marker_reserve = len(
        (
            f"\n{reset}  note: diagnostic truncated; "
            f"{len(rendered)} characters omitted"
        ).encode()
    )
    body_bytes = 0
    selected_count = 0
    selection_order = sorted(
        range(len(groups)),
        key=lambda index: (groups[index].priority, groups[index].order),
    )
    for index in selection_order:
        item = prepared[index]
        group = item.group
        add_scaffold = (
            item.scaffold
            if group.section is not None
            and group.section not in active_sections
            else ""
        )
        added_bytes = (
            (1 if selected_count else 0)
            + (item.scaffold_bytes if add_scaffold else 0)
            + item.text_bytes
        )
        if body_bytes + added_bytes + marker_reserve <= limit:
            retained_flags[index] = True
            selected_count += 1
            body_bytes += added_bytes
            if group.section is not None:
                active_sections.add(group.section)

    retained = assemble_selected_human_groups(prepared, retained_flags)
    # This is the exact net character reduction from the complete human
    # rendering. Compact ellipses and scaffolding are already represented in
    # ``retained`` and therefore cannot make the count drift.
    omitted = max(1, len(rendered) - len(retained))
    marker = (
        f"{reset}  note: diagnostic truncated; {omitted} characters omitted"
    )
    bounded = "\n".join(part for part in (retained, marker) if part)
    if selected_count and len(bounded.encode("utf-8")) <= limit:
        return bounded

    # Caller-selected tiny limits may not hold even the omission notice and a
    # compact headline. Preserve grapheme and ANSI integrity in that case.
    essential = groups[0].text if groups else rendered
    return truncate_with_marker(
        essential,
        limit,
        marker=lambda essential_omitted: (
            f"{reset}\n  note: diagnostic truncated; "
            f"{len(rendered) - len(essential) + essential_omitted} "
            "characters omitted"
        ),
        fallback="diagnostic truncated",
        ansi_safe=True,
    )
