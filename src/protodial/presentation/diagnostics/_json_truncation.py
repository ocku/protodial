"""Select complete rich details within diagnostic JSON budgets."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from ._detail_values import (
    rich_detail_priority,
)
from ._json_base import (
    bounded_diagnostic_json_base,
    minimal_json,
)
from ._json_values import (
    compact_json,
    json_value,
)
from ._render_policy import (
    MAX_TRUNCATED_JSON_NON_DETAIL_CHARS,
    JsonTruncationMetrics,
)
from .models import (
    DiagnosticEvent,
)


def truncate_json_with_bounded_base(
    event: DiagnosticEvent,
    sequence: int,
    limit: int,
    *,
    _metrics: JsonTruncationMetrics | None = None,
) -> str:
    """Bound base fields, then select whole details in near-linear work."""

    details = list(event.details)
    base_budget = min(
        MAX_TRUNCATED_JSON_NON_DETAIL_CHARS,
        max(512, limit // 2),
    )
    base = bounded_diagnostic_json_base(
        event, sequence, base_budget, _metrics=_metrics
    )

    if not details:
        rendered = compact_json(base)
        if len(rendered.encode("utf-8")) <= limit:
            return rendered
        return minimal_json(event, sequence, limit)

    # Normalize and serialize every detail exactly once. Compact JSON is ASCII
    # because ``ensure_ascii`` is enabled, so character and byte accounting
    # are identical from here through final assembly.
    normalized: list[object] = []
    serialized: list[str] = []
    priority_buckets: list[list[int]] = [[], [], [], []]
    for index, detail in enumerate(details):
        normalized_detail = json_value(detail)
        normalized.append(normalized_detail)
        serialized.append(compact_json(normalized_detail))
        priority_buckets[rich_detail_priority(detail)].append(index)
        if _metrics is not None:
            _metrics.normalized_details += 1
            _metrics.serialized_details += 1

    retained = bytearray(len(details))
    retained_count = 0
    retained_serialized_chars = 0
    omitted_count = len(details)
    omitted_serialized_chars = (
        2 + sum(len(item) for item in serialized) + max(0, omitted_count - 1)
    )
    omitted_index_chars = (
        2
        + sum(len(str(index)) for index in range(len(details)))
        + max(0, omitted_count - 1)
    )

    compact_key_lengths = {
        str(key): len(compact_json(str(key))) for key in base
    }
    base_notes = list(cast(Sequence[object], base.get("notes", ())))
    fixed_member_chars = 0
    fixed_member_count = 0
    for key, item in base.items():
        if key == "notes":
            continue
        fixed_member_chars += (
            compact_key_lengths[str(key)] + 1 + len(compact_json(item))
        )
        fixed_member_count += 1

    notes_key_chars = len(compact_json("notes")) + 1
    details_key_chars = len(compact_json("details")) + 1
    base_note_serialized_chars = sum(
        len(compact_json(item)) for item in base_notes
    )

    def array_chars(items_chars: int, count: int) -> int:
        return 2 + items_chars + max(0, count - 1)

    def notes_array_chars(omitted_characters: int | None) -> int | None:
        if omitted_characters is None:
            if not base_notes:
                return None
            return array_chars(base_note_serialized_chars, len(base_notes))
        note = (
            "rich status truncated; "
            f"{omitted_characters} serialized detail characters omitted"
        )
        return array_chars(
            base_note_serialized_chars + len(compact_json(note)),
            len(base_notes) + 1,
        )

    marker_start_chars = len('{"type":"protodial.truncated","preview":')
    marker_between_preview_and_omitted = len(',"characters_omitted":')
    marker_between_omitted_and_indexes = len(',"omitted_detail_indexes":')
    marker_between_indexes_and_count = len(',"original_detail_count":')

    def marker_chars(
        preview: str,
        omitted_characters: int,
        index_array_chars: int,
    ) -> int:
        return (
            marker_start_chars
            + len(compact_json(preview))
            + marker_between_preview_and_omitted
            + len(str(omitted_characters))
            + marker_between_omitted_and_indexes
            + index_array_chars
            + marker_between_indexes_and_count
            + len(str(len(details)))
            + 1
        )

    def object_chars(
        detail_array_chars: int,
        omitted_characters: int | None,
    ) -> int:
        note_chars = notes_array_chars(omitted_characters)
        member_chars = (
            fixed_member_chars + details_key_chars + detail_array_chars
        )
        member_count = fixed_member_count + 1
        if note_chars is not None:
            member_chars += notes_key_chars + note_chars
            member_count += 1
        return 2 + member_chars + max(0, member_count - 1)

    def truncated_candidate_chars(
        *,
        selected_chars: int,
        selected_count: int,
        remaining_chars: int,
        index_chars: int,
        preview: str = "",
    ) -> int:
        omitted_characters = remaining_chars - len(preview)
        marker_size = marker_chars(preview, omitted_characters, index_chars)
        detail_array_size = array_chars(
            selected_chars + marker_size, selected_count + 1
        )
        return object_chars(detail_array_size, omitted_characters)

    # A failed large detail does not stop the scan. Selection follows semantic
    # priority, while final assembly scans source order exactly once.
    for bucket in priority_buckets:
        for index in bucket:
            if _metrics is not None:
                _metrics.selection_candidates += 1
            trial_omitted_count = omitted_count - 1
            trial_omitted_chars = (
                omitted_serialized_chars
                - len(serialized[index])
                - (1 if omitted_count > 1 else 0)
            )
            trial_index_chars = (
                omitted_index_chars
                - len(str(index))
                - (1 if omitted_count > 1 else 0)
            )
            trial_retained_count = retained_count + 1
            trial_retained_chars = retained_serialized_chars + len(
                serialized[index]
            )
            if trial_omitted_count:
                trial_size = truncated_candidate_chars(
                    selected_chars=trial_retained_chars,
                    selected_count=trial_retained_count,
                    remaining_chars=trial_omitted_chars,
                    index_chars=trial_index_chars,
                )
            else:
                trial_size = object_chars(
                    array_chars(trial_retained_chars, trial_retained_count),
                    None,
                )
            if trial_size > limit:
                continue
            retained[index] = 1
            retained_count = trial_retained_count
            retained_serialized_chars = trial_retained_chars
            omitted_count = trial_omitted_count
            omitted_serialized_chars = trial_omitted_chars
            omitted_index_chars = trial_index_chars

    selected: list[object] = []
    omitted_indexes: list[int] = []
    omitted_serialized: list[str] = []
    for index, (value, encoded) in enumerate(
        zip(normalized, serialized, strict=True)
    ):
        if retained[index]:
            selected.append(value)
        else:
            omitted_indexes.append(index)
            omitted_serialized.append(encoded)
    if _metrics is not None:
        _metrics.final_detail_scans += len(details)

    value = dict(base)
    if not omitted_indexes:
        value["details"] = selected
        complete = compact_json(value)
        if len(complete.encode("utf-8")) <= limit:
            return complete
        return minimal_json(event, sequence, limit)

    remaining = "[" + ",".join(omitted_serialized) + "]"
    # Find the longest exact serialized-detail prefix that leaves room for its
    # escaped JSON string and truthful omission count. Candidate probes never
    # rebuild either the survivor list or omitted-index list.
    low = 0
    high = len(remaining)
    best_length = -1
    while low <= high:
        middle = (low + high) // 2
        preview = remaining[:middle]
        if _metrics is not None:
            _metrics.preview_candidates += 1
        trial_size = truncated_candidate_chars(
            selected_chars=retained_serialized_chars,
            selected_count=retained_count,
            remaining_chars=len(remaining),
            index_chars=omitted_index_chars,
            preview=preview,
        )
        if trial_size <= limit:
            best_length = middle
            low = middle + 1
        else:
            high = middle - 1
    if best_length < 0:
        return minimal_json(event, sequence, limit)

    preview = remaining[:best_length]
    characters_omitted = len(remaining) - len(preview)
    selected.append(
        {
            "type": "protodial.truncated",
            "preview": preview,
            "characters_omitted": characters_omitted,
            "omitted_detail_indexes": omitted_indexes,
            "original_detail_count": len(details),
        }
    )
    notes = list(cast(Sequence[object], value.get("notes", ())))
    notes.append(
        "rich status truncated; "
        f"{characters_omitted} serialized detail characters omitted"
    )
    value["notes"] = notes
    value["details"] = selected
    rendered = compact_json(value)
    if len(rendered.encode("utf-8")) <= limit:
        return rendered
    return minimal_json(event, sequence, limit)


def truncate_json(event: DiagnosticEvent, sequence: int, limit: int) -> str:
    """Return valid JSON below *limit* with bounded base and whole details."""
    return truncate_json_with_bounded_base(event, sequence, limit)
