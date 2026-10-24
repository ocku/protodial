"""Structured diagnostic models and rendering."""

from .models import (
    DIAGNOSTIC_REGISTRY,
    DiagnosticDefinition,
    DiagnosticEvent,
    DiagnosticScalar,
    DiagnosticSeverity,
    DiagnosticSink,
    DiagnosticSpan,
    DiagnosticValue,
    NullDiagnosticSink,
    RecordingDiagnosticSink,
)
from .rendering import (
    ColorPolicy,
    DiagnosticFormat,
    StderrDiagnosticSink,
    render_human,
    render_json,
)

__all__ = [
    "DIAGNOSTIC_REGISTRY",
    "DiagnosticEvent",
    "DiagnosticDefinition",
    "DiagnosticFormat",
    "DiagnosticSeverity",
    "DiagnosticSpan",
    "DiagnosticScalar",
    "DiagnosticSink",
    "DiagnosticValue",
    "NullDiagnosticSink",
    "RecordingDiagnosticSink",
    "StderrDiagnosticSink",
    "ColorPolicy",
    "render_human",
    "render_json",
]
