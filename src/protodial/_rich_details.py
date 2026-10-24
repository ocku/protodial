"""Identify standard ``google.rpc`` rich-status detail messages.

This dependency-light module is intentionally outside ``rich_status`` so the
diagnostic bootstrap path can import it even when protobuf is unavailable.
"""

from __future__ import annotations

STANDARD_DETAIL_NAMES = frozenset(
    {
        "BadRequest",
        "DebugInfo",
        "ErrorInfo",
        "Help",
        "LocalizedMessage",
        "PreconditionFailure",
        "QuotaFailure",
        "RequestInfo",
        "ResourceInfo",
        "RetryInfo",
    }
)


def standard_detail_name(identity: object) -> str | None:
    """Return a standard detail's short name from an exact Any identity."""

    full_name = str(identity).rsplit("/", 1)[-1]
    prefix = "google.rpc."
    if not full_name.startswith(prefix):
        return None
    short_name = full_name.removeprefix(prefix)
    if short_name not in STANDARD_DETAIL_NAMES:
        return None
    return short_name
