"""Completion candidates, CLI option metadata, and tolerant argv scanning."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import cast

from ..argument_parser.construction import option_actions


@dataclass(frozen=True, slots=True)
class Candidate:
    """One shell-safe completion value and its display description."""

    value: str
    description: str = ""
    append_space: bool = True
    display: str | None = None

    @property
    def display_text(self) -> str:
        """Return a menu label which keeps the candidate itself visible."""
        if self.display is not None:
            return self.display
        if self.description:
            return f"{self.value}\n\u21b3 {self.description}"
        return self.value


@dataclass(frozen=True, slots=True)
class OptionSpec:
    names: tuple[str, ...]
    description: str
    value_kind: str | None = None
    repeatable: bool = False


_COMPLETION_HINTS = {
    "--help": ("show command help", None),
    "--proto": ("local .proto file", "proto"),
    "--protoset": ("FileDescriptorSet file", "protoset"),
    "--protoset-out": ("write resolved descriptor set", "file"),
    "--use-reflection": ("also use server reflection", None),
    "--proto-path": ("protobuf import root", "directory"),
    "--plaintext": ("disable TLS", None),
    "--insecure": ("disable TLS verification", None),
    "--cacert": ("PEM CA bundle", "file"),
    "--cert": ("PEM client certificate", "file"),
    "--key": ("PEM client private key", "file"),
    "--key-password": ("client private-key password", "text"),
    "--servername": ("TLS server identity", "text"),
    "--authority": ("HTTP/2 authority", "text"),
    "--connect-timeout": ("connection timeout", "number"),
    "--max-time": ("RPC deadline", "number"),
    "--max-recv-message-size": ("maximum response bytes", "number"),
    "--max-send-message-size": ("maximum request bytes", "number"),
    "--compression": ("gRPC compression", "compression"),
    "--keepalive-time": ("HTTP/2 keepalive interval", "number"),
    "--user-agent": ("primary gRPC user agent", "text"),
    "--header": ("shared metadata", "header"),
    "--reflect-header": ("reflection-only metadata", "header"),
    "--color": ("diagnostic color policy", "color"),
    "--diagnostic-format": ("diagnostic encoding", "diagnostic"),
    "--verbose": ("increase diagnostics", None),
    "--version": ("show the protodial version", None),
    "--data": ("request data or @file", "data"),
    "--input-format": ("request encoding", "format"),
    "--output-format": ("response encoding", "format"),
    "--field": ("set a request field", "field"),
    "--field:file": ("read a file into a bytes field", "field_file"),
    "--input-field": ("raw input bytes field", "input_field"),
    "--output-field": ("raw output bytes field", "output_field"),
    "--chunk-size": ("raw stream chunk size", "number"),
    "--allow-unknown-fields": ("allow unknown ProtoJSON fields", None),
    "--emit-defaults": ("emit default-valued fields", None),
    "--max-input-buffer": ("maximum buffered input", "number"),
    "--wait-for-ready": ("wait for an available server", None),
    "--include": ("show response headers", None),
    "--show-trailers": ("show response trailers", None),
    "--show-status": ("show final RPC status", None),
    "--format-error": ("decode structured gRPC errors", None),
    "--rpc-header": ("RPC-only metadata", "header"),
    "--method": ("service/method for a native target", "method"),
    "--show-reflection": ("include reflection services", None),
    "--template": ("print sample request ProtoJSON", None),
    "--template-depth": ("maximum template depth", "number"),
}


VALUE_CHOICES = {
    kind: tuple(cast(Sequence[str], action.choices))
    for action in option_actions("invoke")
    for kind in (
        _COMPLETION_HINTS.get(action.option_strings[-1], ("", None))[1],
    )
    if kind is not None and action.choices is not None
}


@dataclass(slots=True)
class Scan:
    mode: str
    values: dict[str, list[str]]
    flags: set[str]
    positionals: list[str]
    current: str
    value_option: OptionSpec | None = None
    value_prefix: str = ""

    def last(self, *names: str) -> str | None:
        for name in names:
            values = self.values.get(name)
            if values:
                return values[-1]
        return None


@cache
def _options(mode: str) -> tuple[OptionSpec, ...]:
    options: list[OptionSpec] = []
    for action in option_actions(mode):
        if action.dest == "interactive":
            continue  # This option exists only to explain its replacement.
        description, kind = _COMPLETION_HINTS.get(
            action.option_strings[-1], (action.help or "", None)
        )
        if action.nargs != 0 and kind is None:
            kind = "text"
        # argparse exposes action arity but has no public repeatability flag.
        repeatable = isinstance(
            action,
            (argparse._AppendAction, argparse._CountAction),  # pyright: ignore[reportPrivateUsage]
        )
        options.append(
            OptionSpec(
                tuple(action.option_strings), description, kind, repeatable
            )
        )
    return tuple(options)


def _option_map(options: Iterable[OptionSpec]) -> dict[str, OptionSpec]:
    return {name: option for option in options for name in option.names}


def scan(words: Sequence[str], cursor: int | None = None) -> Scan:
    """Scan all argv while treating the cursor word as incomplete."""
    supplied = list(words) or [""]
    if cursor is None:
        cursor = len(supplied) - 1
    cursor = min(max(cursor, 0), len(supplied) - 1)
    current = supplied[cursor]
    mode = (
        supplied[0]
        if supplied[:1]
        and supplied[0]
        in {
            "list",
            "describe",
        }
        else "invoke"
    )
    if supplied[:1] == ["completion"] and (
        len(supplied) == 1
        or supplied[1] in {"bash", "zsh", "install", "-h", "--help"}
        or cursor == 1
        and supplied[1] == ""
    ):
        mode = "completion"
    if mode == "completion":
        positionals = supplied[1:cursor]
        return Scan(mode, {}, set(), positionals, current)

    start = 1 if mode in {"list", "describe"} else 0
    by_name = _option_map(_options(mode))
    values: dict[str, list[str]] = {}
    flags: set[str] = set()
    positionals: list[str] = []
    value_option: OptionSpec | None = None
    index = start
    while index < len(supplied):
        if index == cursor:
            index += 1
            continue
        word = supplied[index]
        if word == "--":
            positionals.extend(
                value
                for value_index, value in enumerate(
                    supplied[index + 1 :], start=index + 1
                )
                if value_index != cursor
            )
            break
        name, separator, value = word.partition("=")
        option = by_name.get(name)
        if option is not None:
            flags.update(option.names)
            if option.value_kind is not None:
                if separator:
                    values.setdefault(option.names[-1], []).append(value)
                else:
                    value_index = index + 1
                    if value_index == cursor:
                        value_option = option
                    elif value_index < len(supplied):
                        values.setdefault(option.names[-1], []).append(
                            supplied[value_index]
                        )
                    index += 1
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        positionals.append(word)
        index += 1

    value_prefix = current
    if value_option is None and current.startswith("-") and "=" in current:
        name, _, value_prefix = current.partition("=")
        value_option = by_name.get(name)
    return Scan(
        mode,
        values,
        flags,
        positionals,
        current,
        value_option,
        value_prefix,
    )


def unique_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    by_value: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.value not in by_value:
            by_value[candidate.value] = candidate
    return sorted(by_value.values(), key=lambda item: item.value)


def filter_candidates(
    candidates: Iterable[Candidate], prefix: str, *, assignment: str = ""
) -> list[Candidate]:
    result: list[Candidate] = []
    for candidate in candidates:
        value = f"{assignment}{candidate.value}"
        if value.startswith(f"{assignment}{prefix}"):
            result.append(
                Candidate(
                    value,
                    candidate.description,
                    candidate.append_space,
                    candidate.display,
                )
            )
    return unique_candidates(result)


def option_candidates(
    scan: Scan, options: Iterable[OptionSpec] | None = None
) -> list[Candidate]:
    """Return one long-form row per option, including any short alias."""
    specs = tuple(_options(scan.mode) if options is None else options)
    preferred_names = tuple(
        next(
            (name for name in option.names if name.startswith("--")),
            option.names[0],
        )
        for option in specs
    )
    width = max((len(name) for name in preferred_names), default=0)
    candidates: list[Candidate] = []
    for option, preferred in zip(specs, preferred_names, strict=True):
        if not option.repeatable and any(
            name in scan.flags for name in option.names
        ):
            continue
        if not any(name.startswith(scan.current) for name in option.names):
            continue
        short = next(
            (
                name
                for name in option.names
                if name.startswith("-") and not name.startswith("--")
            ),
            "",
        )
        candidates.append(
            Candidate(
                preferred,
                option.description,
                display=(
                    f"{preferred:<{width}}  {short:<2}  -- {option.description}"
                ),
            )
        )
    return unique_candidates(candidates)
