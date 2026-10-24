"""Consistent, container-friendly logging for the example servers."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

LOG_LEVEL_ENV = "PROTODIAL_EXAMPLE_LOG_LEVEL"


class _UtcFormatter(logging.Formatter):
    """Render logging timestamps in UTC."""

    converter = time.gmtime


def configure_logging(example: str) -> logging.Logger:
    """Configure stderr logging and return an example-specific logger."""
    requested_level = os.environ.get(LOG_LEVEL_ENV, "INFO").upper()
    level = getattr(logging, requested_level, None)
    invalid_level = not isinstance(level, int)
    if invalid_level:
        level = logging.INFO

    handler = logging.StreamHandler()
    handler.setFormatter(
        _UtcFormatter(
            "%(asctime)sZ %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    logger = logging.getLogger(f"protodial.examples.{example}")
    if invalid_level:
        log_event(
            logger,
            logging.WARNING,
            "logging.invalid_level",
            requested=requested_level,
            fallback="INFO",
        )
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Log one event with deterministically ordered, JSON-quoted fields."""
    suffix = " ".join(
        f"{name}={json.dumps(value, ensure_ascii=True, default=str)}"
        for name, value in sorted(fields.items())
    )
    # Strip JSON's surrounding quotes while retaining its ASCII escapes. This
    # prevents line separators, bidi markers, and terminal controls in an
    # externally supplied event name from forging the plain-text log stream.
    safe_event = json.dumps(str(event), ensure_ascii=True)[1:-1]
    message = f"event={safe_event}"
    if suffix:
        message = f"{message} {suffix}"
    logger.log(level, message)


def elapsed_ms(started_at: float) -> float:
    """Return monotonic elapsed time in milliseconds."""
    return round((time.monotonic() - started_at) * 1000, 3)
