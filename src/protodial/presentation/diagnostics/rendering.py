"""Public rendering facade and atomic stderr diagnostic sink."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Mapping
from typing import TextIO

from ._details import human_detail as _human_detail
from ._generic_truncation import (
    truncate_generic_human as truncate_generic_human,
)
from ._human import render_human as render_human
from ._human_truncation import (
    human_truncation_notice as _human_truncation_notice,
)
from ._human_truncation import truncate_human as _truncate_human
from ._json import render_json as render_json
from ._json_base import (
    bounded_diagnostic_json_base as _bounded_diagnostic_json_base,
)
from ._json_truncation import truncate_json
from ._json_truncation import (
    truncate_json_with_bounded_base as _truncate_json_with_bounded_base,
)
from ._render_policy import (
    MAX_RENDERED_DIAGNOSTIC_CHARS as MAX_RENDERED_DIAGNOSTIC_CHARS,
)
from ._render_policy import (
    MAX_RICH_DETAIL_PREVIEW_CHARS as MAX_RICH_DETAIL_PREVIEW_CHARS,
)
from ._render_policy import (
    MAX_TRUNCATED_JSON_NON_DETAIL_CHARS as MAX_TRUNCATED_JSON_NON_DETAIL_CHARS,
)
from ._render_policy import SCHEMA as SCHEMA
from ._render_policy import ColorPolicy as ColorPolicy
from ._render_policy import DiagnosticFormat as DiagnosticFormat
from ._render_policy import HumanTruncationMetrics as HumanTruncationMetrics
from ._render_policy import JsonTruncationMetrics as JsonTruncationMetrics
from ._rpc_truncation import (
    truncate_rpc_failure_human as truncate_rpc_failure_human,
)
from ._terminal import abbreviate_terminal_text as abbreviate_terminal_text
from ._terminal import display_width as _display_width
from ._terminal import terminal_cell_width as terminal_cell_width
from ._terminal import terminal_width
from ._terminal import wrap_cells as _wrap_cells
from ._terminal import wrap_terminal_cells as wrap_terminal_cells
from ._text import format_duration as format_duration
from .models import (
    DIAGNOSTIC_EVENT_ALIASES,
    DIAGNOSTIC_REGISTRY,
    DiagnosticEvent,
)

__all__ = [
    "ColorPolicy",
    "DiagnosticFormat",
    "HumanTruncationMetrics",
    "JsonTruncationMetrics",
    "MAX_RENDERED_DIAGNOSTIC_CHARS",
    "MAX_RICH_DETAIL_PREVIEW_CHARS",
    "MAX_TRUNCATED_JSON_NON_DETAIL_CHARS",
    "SCHEMA",
    "StderrDiagnosticSink",
    "abbreviate_terminal_text",
    "format_duration",
    "render_human",
    "render_json",
    "terminal_cell_width",
    "truncate_generic_human",
    "truncate_rpc_failure_human",
    "wrap_terminal_cells",
    "_bounded_diagnostic_json_base",
    "_display_width",
    "_human_detail",
    "_human_truncation_notice",
    "_truncate_human",
    "_truncate_json_with_bounded_base",
    "_wrap_cells",
]


class StderrDiagnosticSink:
    """Own formatting, sequencing, and atomic stream writes."""

    def __init__(
        self,
        verbosity: int = 1,
        *,
        stream: TextIO | None = None,
        diagnostic_format: DiagnosticFormat | str = DiagnosticFormat.HUMAN,
        color: ColorPolicy | str = ColorPolicy.AUTO,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.verbosity = max(0, verbosity)
        self.stream = stream if stream is not None else sys.stderr
        self.diagnostic_format = DiagnosticFormat(diagnostic_format)
        self.color_policy = ColorPolicy(color)
        self.environ = os.environ if environ is None else environ
        self.sequence = 0
        self._lock = threading.Lock()

    def _color_enabled(self) -> bool:
        if self.diagnostic_format is DiagnosticFormat.JSON:
            return False
        if self.color_policy is ColorPolicy.ALWAYS:
            return True
        if self.color_policy is ColorPolicy.NEVER:
            return False
        try:
            tty = self.stream.isatty()
        except (AttributeError, OSError):
            tty = False
        return (
            tty
            and self.environ.get("TERM") != "dumb"
            and "NO_COLOR" not in self.environ
        )

    def emit(self, event: DiagnosticEvent) -> None:
        """Render an eligible diagnostic and flush one atomic event write."""
        event.validate()
        if event.level > self.verbosity:
            return
        definition = DIAGNOSTIC_REGISTRY.get(event.code) if event.code else None
        aliases = DIAGNOSTIC_EVENT_ALIASES.get(event.code or "", frozenset())
        if (
            definition is not None
            and definition.event != event.name
            and event.name not in aliases
        ):
            raise ValueError(
                f"diagnostic {event.code} must use event "
                f"{definition.event!r}, not {event.name!r}"
            )
        with self._lock:
            next_sequence = self.sequence + 1
            color_enabled = self._color_enabled()
            width = terminal_width(self.stream)
            if self.diagnostic_format is DiagnosticFormat.JSON:
                rendered = render_json(event, next_sequence)
            else:
                rendered = render_human(
                    event,
                    color=color_enabled,
                    width=width,
                )
                # Metadata phase events intentionally have no human rendering
                # when every entry is empty or whitespace-only.  Do not turn
                # that absence into a blank stderr line (or a consumed human
                # sequence number); JSON still records the phase event above.
                if not rendered.strip():
                    return
            # Leave room for the required newline while keeping the complete
            # write strictly below the documented 64 KiB guard.
            content_limit = MAX_RENDERED_DIAGNOSTIC_CHARS - 2
            if len(rendered.encode("utf-8")) > content_limit:
                if self.diagnostic_format is DiagnosticFormat.JSON:
                    rendered = truncate_json(
                        event, next_sequence, content_limit
                    )
                elif event.name == "rpc.failed":
                    rendered = truncate_rpc_failure_human(
                        event,
                        content_limit,
                        color=color_enabled,
                        width=width,
                    )
                else:
                    rendered = truncate_generic_human(
                        event,
                        rendered,
                        content_limit,
                        color=color_enabled,
                        width=width,
                    )
            self.stream.write(rendered + "\n")
            self.stream.flush()
            self.sequence = next_sequence
