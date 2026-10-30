"""Sanitization, styling, and compact status text."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from ..sanitization import escape_control_characters
from .models import (
    DiagnosticEvent,
    DiagnosticValue,
)

_STYLE = {
    "error": "1;31",
    "warning": "1;33",
    "info": "1;36",
    "status": "1;32",
    "location": "34",
    "gutter": "2",
    "help": "36",
    "strong": "1",
}


def style(text: str, role: str, enabled: bool) -> str:
    return f"\x1b[{_STYLE[role]}m{text}\x1b[0m" if enabled else text


def safe(value: object) -> str:
    return escape_control_characters(value)


def style_semantic_text(text: str, color: bool) -> str:
    if not color:
        return text
    return re.sub(
        r"(`[^`]*`)",
        lambda match: style(match.group(1), "strong", True),
        text,
    )


def format_duration(seconds: float) -> str:
    """Format seconds with a compact, three-significant-digit unit."""
    if not math.isfinite(seconds) or seconds < 0:
        return "unknown duration"
    # Select the unit after accounting for three-significant-digit rounding.
    # Otherwise 999.5 us would be rendered as the contradictory ``1e+03 us``
    # instead of being promoted to ``1 ms`` (and likewise for milliseconds).
    if seconds < 0.0009995:
        return f"{_three_significant_digits(seconds * 1_000_000)} us"
    if seconds < 0.001:
        return "1 ms"
    if seconds < 0.9995:
        return f"{_three_significant_digits(seconds * 1000)} ms"
    if seconds < 1:
        return "1 s"
    return f"{_three_significant_digits(seconds)} s"


def _three_significant_digits(value: float) -> str:
    """Render a finite value with three significant digits and no exponent."""
    if value == 0:
        return "0"
    magnitude = math.floor(math.log10(abs(value)))
    decimal_places = max(0, 2 - magnitude)
    rendered = f"{value:.{decimal_places}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def count(value: int) -> str:
    return f"{value} message" + ("" if value == 1 else "s")


def duration_suffix(value: DiagnosticValue | None) -> str:
    """Render an optional numeric duration as a headline suffix."""
    if isinstance(value, (int, float)):
        return f" in {format_duration(float(value))}"
    return ""


def counter(
    fields: Mapping[str, DiagnosticValue], canonical: str, legacy: str
) -> int | None:
    """Read a counter from either additive schema-v1 spelling."""
    for name in (canonical, legacy):
        value = fields.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def status_parts(event: DiagnosticEvent) -> tuple[str, int | None]:
    """Read an RPC status from canonical or compatibility context fields."""
    status = event.fields.get("grpc_status")
    if isinstance(status, Mapping):
        raw_name = status.get("name", "UNKNOWN")
        name = str(raw_name)
        raw_number = status.get("number")
        number = (
            raw_number
            if isinstance(raw_number, int) and not isinstance(raw_number, bool)
            else None
        )
        return name, number
    raw_name = event.fields.get("status", "UNKNOWN")
    return str(raw_name), None
