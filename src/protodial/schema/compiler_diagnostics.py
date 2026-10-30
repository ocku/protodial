"""Translate protoc output into source-located errors and warnings."""

from __future__ import annotations

import pathlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..errors import ProtoCompilationError, add_error_note
from .source_locations import utf8_byte_column_offset


@dataclass(frozen=True)
class _CompilerDiagnostic:
    """One source-located diagnostic parsed from protoc output."""

    path: str
    line: int | None
    column: int | None
    message: str
    raw: str


_COMPILER_LOCATION = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?P<message>.*)$"
)
_NATIVE_LOG_LINE = re.compile(r"^[IWEF]\d{4}\s+.*?\]\s*(?P<message>.*)$")


def _compiler_lines(output_parts: Sequence[str]) -> tuple[str, ...]:
    """Return useful, de-duplicated compiler output without native chatter."""
    lines: list[str] = []
    for part in output_parts:
        for raw_line in part.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(
                "WARNING: All log messages before absl::InitializeLog()"
            ):
                continue
            native = _NATIVE_LOG_LINE.match(line)
            if native is not None:
                line = native.group("message").strip()
            if (
                "Other threads are currently calling into gRPC" in line
                or line.startswith("FD from fork parent still in poll list")
            ):
                continue
            if line:
                lines.append(line)
    return tuple(dict.fromkeys(lines))


def _compiler_diagnostic(
    line: str, roots: Sequence[pathlib.Path]
) -> _CompilerDiagnostic | None:
    match = _COMPILER_LOCATION.match(line)
    if match is None:
        return None
    raw_path = match.group("path")
    candidate = pathlib.Path(raw_path)
    if not candidate.is_absolute():
        candidate = next(
            (
                root / candidate
                for root in roots
                if (root / candidate).is_file()
            ),
            candidate,
        )
    return _CompilerDiagnostic(
        str(candidate),
        int(match.group("line")),
        (
            int(match.group("column"))
            if match.group("column") is not None
            else None
        ),
        match.group("message").strip() or "proto compiler rejected this input",
        line,
    )


def compiler_error(
    output_parts: Sequence[str], roots: Sequence[pathlib.Path]
) -> ProtoCompilationError:
    """Translate compiler output into a located schema error."""
    lines = _compiler_lines(output_parts)
    parsed = tuple(
        diagnostic
        for line in lines
        if (diagnostic := _compiler_diagnostic(line, roots)) is not None
    )
    primary = parsed[0] if parsed else None
    error = ProtoCompilationError(
        "proto compilation failed",
        path=primary.path if primary is not None else "<proto compiler>",
        line=primary.line if primary is not None else None,
        column=primary.column if primary is not None else None,
        reason=primary.message if primary is not None else None,
        help="fix the reported proto syntax or import error and retry",
    )
    primary_raw = primary.raw if primary is not None else None
    for line in lines:
        if line != primary_raw:
            add_error_note(error, f"compiler: {line}")
    if not getattr(error, "_protodial_notes", ()):
        if primary is None:
            add_error_note(
                error,
                "compiler did not provide additional error context",
            )
    return error


def _source_span(
    diagnostic: _CompilerDiagnostic,
) -> tuple[str | None, int | None, int | None]:
    """Return source text and offsets for a compiler diagnostic."""
    if diagnostic.line is None:
        return None, None, None
    try:
        lines = (
            pathlib.Path(diagnostic.path)
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except (OSError, UnicodeError):
        return None, None, None
    if not 0 < diagnostic.line <= len(lines):
        return None, None, None
    text = lines[diagnostic.line - 1]
    start = utf8_byte_column_offset(text, diagnostic.column or 1)
    return text, start, min(len(text), start + 1)


def _warning_copy(message: str) -> tuple[str, str]:
    """Return a semantic warning headline and actionable help."""
    lowered = message.casefold()
    if "no syntax specified" in lowered or (
        "no edition or syntax specified" in lowered
    ):
        return (
            "proto source does not declare its syntax",
            'add `syntax = "proto3";` near the start of the file',
        )
    if "import" in lowered and "unused" in lowered:
        return (
            "proto source has an unused import",
            "remove the unused import or use a type that it defines",
        )
    return (
        "proto compiler accepted the schema with a warning",
        "review the compiler warning and update the source if needed",
    )


def _warning_diagnostic(
    line: str, roots: Sequence[pathlib.Path]
) -> _CompilerDiagnostic | None:
    """Parse a warning location, including filename-only syntax warnings."""
    diagnostic = _compiler_diagnostic(line, roots)
    if diagnostic is not None:
        return diagnostic
    match = re.search(r"proto file: (?P<path>.+?)\. Please\b", line)
    if match is None:
        return None
    raw_path = match.group("path")
    candidate = next(
        (root / raw_path for root in roots if (root / raw_path).is_file()),
        pathlib.Path(raw_path),
    )
    return _CompilerDiagnostic(str(candidate), 1, 1, line, line)


def emit_compiler_warnings(
    output_parts: Sequence[str], roots: Sequence[pathlib.Path]
) -> None:
    """Emit successful-compilation warnings through the active sink."""
    lines = _compiler_lines(output_parts)
    if not lines:
        return
    from ..presentation.diagnostics.context import current_sink
    from ..presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
        DiagnosticSpan,
    )

    sink = current_sink()
    if sink is None:
        return
    for line in lines:
        diagnostic = _warning_diagnostic(line, roots)
        reason = diagnostic.message if diagnostic is not None else line
        headline, help_item = _warning_copy(reason)
        spans: tuple[DiagnosticSpan, ...] = ()
        if diagnostic is not None:
            text, start, end = _source_span(diagnostic)
            spans = (
                DiagnosticSpan(
                    diagnostic.path,
                    line=diagnostic.line,
                    column=diagnostic.column,
                    text=text,
                    start=start,
                    end=end,
                    label="compiler warning",
                ),
            )
        sink.emit(
            DiagnosticEvent(
                "schema.compiler_warning",
                headline,
                level=0,
                severity=DiagnosticSeverity.WARNING,
                code="W2001",
                spans=spans,
                causes=(reason,),
                help=(help_item,),
            )
        )
