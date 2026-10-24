"""Sanitize untrusted values for terminal presentation."""

from __future__ import annotations

import unicodedata


def escape_control_characters(value: object) -> str:
    """Render a value without allowing terminal control-character injection."""

    text = str(value)
    escaped: list[str] = []
    named = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    for character in text:
        replacement = named.get(character)
        if replacement is not None:
            escaped.append(replacement)
            continue
        codepoint = ord(character)
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            if codepoint <= 0xFF:
                escaped.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                escaped.append(f"\\u{codepoint:04x}")
            else:
                escaped.append(f"\\U{codepoint:08x}")
        else:
            escaped.append(character)
    return "".join(escaped)
