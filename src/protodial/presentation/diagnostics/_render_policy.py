"""Shared diagnostic rendering policies and work counters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

SCHEMA = "protodial.diagnostic.v1"

MAX_RENDERED_DIAGNOSTIC_CHARS = 64 * 1024

MAX_RICH_DETAIL_PREVIEW_CHARS = 8 * 1024

MAX_TRUNCATED_JSON_NON_DETAIL_CHARS = 16 * 1024


class DiagnosticFormat(str, Enum):
    """Supported diagnostic stream encodings."""

    HUMAN = "human"
    JSON = "json"


class ColorPolicy(str, Enum):
    """Supported human diagnostic color policies."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


@dataclass
class JsonTruncationMetrics:
    """Deterministic work counters for bounded-JSON complexity tests."""

    base_candidates: int = 0
    context_entries: int = 0
    normalized_details: int = 0
    serialized_details: int = 0
    selection_candidates: int = 0
    final_detail_scans: int = 0
    preview_candidates: int = 0


@dataclass
class HumanTruncationMetrics:
    """Deterministic work counters for bounded-human complexity tests."""

    rendered_groups: int = 0
    selection_candidates: int = 0
    partial_line_candidates: int = 0
    final_group_scans: int = 0


@dataclass(frozen=True)
class DetailTruncationItem:
    """One semantic rich-detail item plus its optional shared heading."""

    text: str
    heading: str = ""
