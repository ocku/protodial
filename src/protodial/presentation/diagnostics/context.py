"""Command-scoped diagnostic sink propagation."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from .models import DiagnosticSink

_ACTIVE_SINK: ContextVar[DiagnosticSink | None] = ContextVar(
    "protodial_diagnostic_sink", default=None
)


def current_sink() -> DiagnosticSink | None:
    """Return the sink installed at the CLI boundary, when present."""
    return _ACTIVE_SINK.get()


@contextmanager
def use_sink(sink: DiagnosticSink) -> Generator[None, None, None]:
    """Install one sink for all layers of the current command."""
    token = _ACTIVE_SINK.set(sink)
    try:
        yield
    finally:
        _ACTIVE_SINK.reset(token)
