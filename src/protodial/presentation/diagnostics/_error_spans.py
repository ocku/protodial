"""Locate operational failures in command arguments and source inputs."""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

from ...commands.models import Command
from ...errors import (
    DescriptorExportError,
    LocalFileError,
    ProtoCompilationError,
    SchemaInputError,
    TlsMaterialSource,
)
from ...schema.source_locations import utf8_byte_column_offset
from .models import (
    DiagnosticSpan,
)


def _source_line(contents: str, line: int) -> str | None:
    """Read a source line, including an empty final line after a newline."""
    lines = contents.splitlines()
    if not lines or contents.endswith(("\n", "\r")):
        lines.append("")
    return lines[line - 1] if 0 < line <= len(lines) else None


def _file_source_line(path: str, line: int | None) -> str | None:
    """Read an available UTF-8 excerpt without masking the original error."""
    if line is None:
        return None
    try:
        return _source_line(
            pathlib.Path(path).read_text(encoding="utf-8"), line
        )
    except (OSError, UnicodeError):
        return None


def input_span(command: Command, message: str) -> DiagnosticSpan | None:
    """Locate a parse failure in literal, file, or standard input."""
    import re

    options = command.invoke_options
    if options is None or options.data is None:
        return None
    match = re.search(r"\bat line (\d+), column (\d+)(?=\s*:|$)", message)
    if match is None:
        match = re.match(
            r"invalid text request \d+:\s*(\d+):(\d+)\s*:", message
        )
    line = int(match.group(1)) if match else None
    column = int(match.group(2)) if match else None
    data = options.data
    source = "--data"
    text: str | None = None
    if data in ("-", "@-"):
        source = "<stdin>"
    elif data.startswith("@"):
        source = data[1:]
        text = _file_source_line(source, line)
    else:
        text = _source_line(data, line if line is not None else 1)
    start = (
        min(len(text), max(0, (column or 1) - 1)) if text is not None else None
    )
    end = (
        None
        if start is None
        else start
        if start == len(text or "")
        else start + 1
    )
    return DiagnosticSpan(
        source,
        line=line,
        column=column,
        text=text,
        start=start,
        end=end,
        label="invalid input",
    )


def option_path_span(
    argv: Sequence[str],
    error: LocalFileError | DescriptorExportError | TlsMaterialSource,
    *,
    label: str = "path could not be read",
) -> DiagnosticSpan:
    """Locate the exact configured path, including repeated options."""
    matches: list[DiagnosticSpan] = []
    option_strings = getattr(error, "option_strings", (error.argument,))
    option_value = getattr(error, "option_value", error.path)

    def offsets(text: str) -> tuple[int, int] | None:
        if text != option_value:
            return None
        start = text.rfind(error.path)
        if start < 0:
            return None
        return start, start + len(error.path)

    for index, token in enumerate(argv):
        for option in option_strings:
            if token == option and index + 1 < len(argv):
                value = argv[index + 1]
                span_offsets = offsets(value)
                if span_offsets is not None:
                    matches.append(
                        DiagnosticSpan(
                            f"argument {index + 2}",
                            text=value,
                            start=span_offsets[0],
                            end=span_offsets[1],
                            label=label,
                        )
                    )
                break
            prefix = option + "=" if option.startswith("--") else option
            if token.startswith(prefix) and token != option:
                value = token[len(prefix) :]
                span_offsets = offsets(value)
                if span_offsets is not None:
                    matches.append(
                        DiagnosticSpan(
                            f"argument {index + 1}",
                            text=token,
                            start=len(prefix) + span_offsets[0],
                            end=len(prefix) + span_offsets[1],
                            label=label,
                        )
                    )
                break
    if matches:
        try:
            return matches[error.occurrence]
        except IndexError:
            pass
    return DiagnosticSpan(error.path, label=label)


def operand_span(
    argv: Sequence[str], value: str, label: str
) -> DiagnosticSpan | None:
    """Locate an operation operand, including a method embedded in a URL."""
    if not value:
        return None
    for index in range(len(argv) - 1, -1, -1):
        token = argv[index]
        if token == value:
            return DiagnosticSpan(
                f"argument {index + 1}",
                text=token,
                start=0,
                end=len(value),
                label=label,
            )
    for index in range(len(argv) - 1, -1, -1):
        token = argv[index]
        start = token.find(value)
        if start < 0 or (start > 0 and token[start - 1] not in {"/", "="}):
            continue
        return DiagnosticSpan(
            f"argument {index + 1}",
            text=token,
            start=start,
            end=start + len(value),
            label=label,
        )
    return None


def schema_input_span(error: SchemaInputError) -> DiagnosticSpan:
    """Build a source span for a schema decoder or compiler failure."""
    text = _file_source_line(error.path, error.line)
    start = None
    if text is not None and error.column is not None:
        start = (
            utf8_byte_column_offset(text, error.column)
            if isinstance(error, ProtoCompilationError)
            else min(len(text), max(0, error.column - 1))
        )
    end = (
        None
        if start is None
        else start
        if start == len(text or "")
        else start + 1
    )
    return DiagnosticSpan(
        error.path,
        line=error.line,
        column=error.column,
        text=text,
        start=start,
        end=end,
        label="invalid schema input",
    )
