"""Translate compiler source locations into Python string offsets."""

from __future__ import annotations


def utf8_byte_column_offset(text: str, column: int) -> int:
    """Convert a one-based UTF-8 byte column to a bounded string offset.

    Protoc reports byte positions rather than Unicode code-point positions.
    A position within a multibyte sequence still identifies that character;
    columns at or beyond the encoded end identify an insertion at end-of-line.
    """
    byte_offset = max(0, column - 1)
    consumed = 0
    for offset, character in enumerate(text):
        width = len(character.encode("utf-8"))
        if byte_offset < consumed + width:
            return offset
        consumed += width
    return len(text)
