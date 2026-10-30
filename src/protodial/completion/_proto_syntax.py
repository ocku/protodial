"""Tolerant lexical proto inspection for completion name discovery."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ._arguments import Candidate

PACKAGE = re.compile(r"\bpackage\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;")
_INTEGER_LITERAL = r"(?:0[xX][0-9A-Fa-f]+|\d+)"
_BLOCK = re.compile(
    r"\b(?P<kind>service|message|enum|oneof|extend|group)\s+"
    r"(?P<name>[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*"
    r"(?:=\s*" + _INTEGER_LITERAL + r"\s*(?:\[[^{}]*?\]\s*)?)?\{"
)
RPC = re.compile(
    r"\brpc\s+(?P<name>[A-Za-z_]\w*)\s*\(\s*"
    r"(?P<client>stream\s+)?[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\)"
    r"\s*returns\s*\(\s*(?P<server>stream\s+)?"
    r"[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\)"
)
FIELD = re.compile(
    r"(?:^|;)\s*(?:(?:optional|required|repeated)\s+)?"
    r"(?!group\b)"
    r"(?:map\s*<[^;{}]+>|[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+"
    r"([A-Za-z_]\w*)\s*=\s*" + _INTEGER_LITERAL + r"\b",
    re.MULTILINE,
)
_ENUM_VALUE = re.compile(
    r"(?:^|;)\s*([A-Za-z_]\w*)\s*=\s*-?" + _INTEGER_LITERAL + r"\b",
    re.MULTILINE,
)


def mask_comments_and_strings(text: str) -> str:
    """Replace comments and quoted strings while preserving line breaks."""
    result: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if character == "/" and following == "/":
            end = text.find("\n", index + 2)
            if end < 0:
                result.append(" ")
                break
            result.append("\n")
            index = end + 1
            continue
        if character == "/" and following == "*":
            end = text.find("*/", index + 2)
            end = len(text) if end < 0 else end + 2
            result.append(" ")
            result.append("\n" * text.count("\n", index, end))
            index = end
            continue
        if character in {'"', "'"}:
            quote = character
            end = index + 1
            while end < len(text):
                if text[end] == "\\" and end + 1 < len(text):
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    break
                end += 1
            result.append(" ")
            result.append("\n" * text.count("\n", index, end))
            index = end
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _brace_body(text: str, opening: int) -> tuple[str, int] | None:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index], index + 1
    return None


def direct_statements(body: str) -> str:
    """Mask nested declarations while preserving direct source statements."""
    result = list(body)
    depth = 0
    for index, character in enumerate(body):
        if character == "{":
            depth += 1
        if depth and character != "\n":
            # A declaration block ends a statement even without a semicolon.
            # Keep that boundary so same-line fields after it remain visible.
            result[index] = ";" if character == "}" and depth == 1 else " "
        if character == "}":
            depth = max(0, depth - 1)
    return "".join(result)


def direct_blocks(text: str) -> Iterable[tuple[str, str, str]]:
    """Yield direct declaration blocks from tolerant masked proto text."""
    index = 0
    depth = 0
    while index < len(text):
        character = text[index]
        if depth == 0:
            match = _BLOCK.match(text, index)
            if match is not None:
                block = _brace_body(text, match.end() - 1)
                if block is None:
                    return
                body, end = block
                yield match.group("kind"), match.group("name"), body
                index = end
                continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
        index += 1


def enum_symbols(body: str, name: str) -> list[Candidate]:
    """Build completion names for an enum and its scoped values."""
    result = [Candidate(name, "enum")]
    scope, _, _ = name.rpartition(".")
    result.extend(
        Candidate(
            f"{scope}.{match.group(1)}" if scope else match.group(1),
            "enum value",
        )
        for match in _ENUM_VALUE.finditer(direct_statements(body))
    )
    return result


def message_symbols(body: str, name: str) -> list[Candidate]:
    """Build completion names for a message and its declarations."""
    result = [Candidate(name, "message")]
    result.extend(
        Candidate(f"{name}.{match.group(1)}", "field")
        for match in FIELD.finditer(direct_statements(body))
    )
    for kind, child_name, child_body in direct_blocks(body):
        qualified = f"{name}.{child_name}"
        if kind == "message":
            result.extend(message_symbols(child_body, qualified))
        elif kind == "group":
            result.extend(group_symbols(child_body, qualified))
        elif kind == "enum":
            result.extend(enum_symbols(child_body, qualified))
        elif kind == "oneof":
            result.append(Candidate(qualified, "oneof"))
            result.extend(
                Candidate(f"{name}.{match.group(1)}", "field")
                for match in FIELD.finditer(direct_statements(child_body))
            )
        elif kind == "extend":
            result.extend(extension_symbols(child_body, name))
    return result


def group_symbols(
    body: str, name: str, field_kind: str = "field"
) -> list[Candidate]:
    """Build the message and lowercase field names of a proto2 group."""
    parent, separator, child = name.rpartition(".")
    field_name = f"{parent}{separator}{child.lower()}"
    return [
        Candidate(field_name, field_kind),
        *message_symbols(body, name),
    ]


def extension_symbols(body: str, scope: str) -> list[Candidate]:
    """Build ordinary and group extension names in their defining scope."""
    prefix = f"{scope}." if scope else ""
    result = [
        Candidate(f"{prefix}{match.group(1)}", "extension")
        for match in FIELD.finditer(direct_statements(body))
    ]
    for kind, name, child_body in direct_blocks(body):
        if kind == "group":
            result.extend(
                group_symbols(child_body, f"{prefix}{name}", "extension")
            )
    return result
