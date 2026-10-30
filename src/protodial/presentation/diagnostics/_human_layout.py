"""Human diagnostic headlines, annotations, and metadata blocks."""

from __future__ import annotations

from ..rpc import render_metadata
from ._terminal import (
    abbreviate_cells,
    display_width,
    wrap_cells,
)
from ._text import (
    safe,
    style,
    style_semantic_text,
)
from .models import (
    DiagnosticEvent,
)


def metadata_lines(
    event: DiagnosticEvent, *, color: bool, width: int
) -> list[str]:
    """Render metadata as a curl-like directional wire block."""
    marker = {
        "rpc.started": ">",
        "rpc.headers": "<",
        "rpc.trailers": "<",
    }.get(event.name, "*")
    heading = {
        "rpc.started": "request metadata",
        "rpc.headers": "response headers",
        "rpc.trailers": "response trailers",
    }.get(event.name, "metadata")
    visible_metadata = [
        (key, value)
        for key, value in event.metadata
        if key.strip()
        and (bool(value) if isinstance(value, bytes) else bool(value.strip()))
    ]
    if not visible_metadata:
        return []
    marker_role = "location" if marker in {">", "<"} else "gutter"
    prefix = style(marker, marker_role, color)
    marker_width = display_width(marker) + 1
    content_width = max(1, width - marker_width)
    heading_lines = wrap_cells(heading, content_width)
    lines = [
        f"{prefix} {style(heading_line, 'strong', color)}"
        for heading_line in heading_lines
    ]
    # Keep the public metadata renderer and diagnostic sink on one wrapping
    # grammar.  Every physical continuation repeats the direction marker and
    # uses a stable two-space continuation indent inside the wire block.
    for content_line in render_metadata(visible_metadata, width=content_width):
        lines.append(f"{prefix} {content_line}")
    # curl terminates a header block with its bare direction marker. Keep that
    # familiar boundary while naming the gRPC metadata phase explicitly.
    if marker in {">", "<"}:
        lines.append(prefix)
    return lines


def annotation_lines(
    label: str,
    value: str,
    *,
    role: str,
    color: bool,
    width: int,
    wrap: bool = True,
) -> list[str]:
    if not value.strip():
        return []
    plain_prefix = f"  {label}: "
    styled_prefix = f"  {style(label, role, color)}: "
    indent_width = display_width(plain_prefix)
    safe_value = safe(value)
    wrapped = (
        wrap_cells(safe_value, max(1, width - indent_width))
        if wrap
        else [safe_value]
    )
    return [
        styled_prefix + style_semantic_text(wrapped[0], color),
        *(
            " " * indent_width + style_semantic_text(line, color)
            for line in wrapped[1:]
        ),
    ]


def rpc_annotation_lines(
    label: str,
    value: str,
    *,
    role: str,
    color: bool,
    width: int,
    indent: int = 2,
    value_role: str | None = None,
) -> list[str]:
    """Render a compact RPC label without the source-diagnostic gutter."""
    if not value.strip():
        return []
    padding = " " * indent
    plain_prefix = f"{padding}{label}: "
    styled_prefix = f"{padding}{style(label, role, color)}: "
    indent_width = display_width(plain_prefix)
    wrapped = wrap_cells(safe(value), max(1, width - indent_width))

    def style_value(text: str) -> str:
        if value_role is not None:
            return style(text, value_role, color)
        return style_semantic_text(text, color)

    return [
        styled_prefix + style_value(wrapped[0]),
        *(" " * indent_width + style_value(line) for line in wrapped[1:]),
    ]


def headline_lines(
    event: DiagnosticEvent,
    message: str,
    *,
    color: bool,
    width: int,
    wrap: bool = True,
) -> list[str]:
    severity = event.severity.value
    label: str | None = None
    if severity == "error":
        code = (
            abbreviate_cells(
                safe(event.code),
                max(1, width * 8),
                max_bytes=2048,
                max_codepoints=1024,
            )
            if event.code
            else ""
        )
        label = "protodial" + (f": ({code})" if code else ":")
        plain_prefix = f"{label} "
        styled_prefix = f"{style(label, severity, color)} "
    elif event.name == "rpc.completed":
        # A terminal non-OK status is the warning-colored counterpart to
        # ``* OK``, not a prose warning. Keep its stable W-code in machine
        # diagnostics without adding warning boilerplate to the human line.
        plain_prefix = "* "
        styled_prefix = style("*", severity, color) + " "
    elif severity == "warning":
        code = (
            abbreviate_cells(
                safe(event.code),
                max(1, width * 8),
                max_bytes=2048,
                max_codepoints=1024,
            )
            if event.code
            else ""
        )
        label = "* warning" + (f" ({code})" if code else "")
        plain_prefix = f"{label}: "
        styled_prefix = f"{style(label, severity, color)}: "
    else:
        plain_prefix = "* "
        styled_prefix = style("*", severity, color) + " "
    indent_width = display_width(plain_prefix)

    if not wrap:
        return [styled_prefix + style_semantic_text(message, color)]

    # Extension codes are intentionally unrestricted. If one makes the label
    # consume most (or all) of the terminal, a conventional hanging indent
    # either exceeds ``width`` or leaves a one-cell message column followed by
    # an enormous continuation indent. Put such a label and its message in
    # separate, compact blocks. Registered E####/W#### prefixes stay on the
    # established single-line layout at every supported terminal width.
    if label is not None and indent_width > width // 2:
        label_lines = wrap_cells(plain_prefix.rstrip(), max(1, width))
        message_indent = min(2, max(0, width - 1))
        message_lines = wrap_cells(message, max(1, width - message_indent))
        return [
            *(style(line, severity, color) for line in label_lines),
            *(
                " " * message_indent + style_semantic_text(line, color)
                for line in message_lines
            ),
        ]

    wrapped = wrap_cells(message, max(1, width - indent_width))
    return [
        styled_prefix + style_semantic_text(wrapped[0], color),
        *(
            " " * indent_width + style_semantic_text(line, color)
            for line in wrapped[1:]
        ),
    ]
