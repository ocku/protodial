"""Completion candidates, CLI option metadata, and tolerant argv scanning."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


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


_SHARED_OPTIONS = (
    OptionSpec(("-h", "--help"), "show command help"),
    OptionSpec(("--proto",), "local .proto file", "proto", True),
    OptionSpec(("--protoset",), "FileDescriptorSet file", "protoset", True),
    OptionSpec(("--protoset-out",), "write resolved descriptor set", "file"),
    OptionSpec(("--use-reflection",), "also use server reflection"),
    OptionSpec(("--proto-path",), "protobuf import root", "directory", True),
    OptionSpec(("--plaintext",), "disable TLS"),
    OptionSpec(("-k", "--insecure"), "disable TLS verification"),
    OptionSpec(("--cacert",), "PEM CA bundle", "file", True),
    OptionSpec(("--cert",), "PEM client certificate", "file"),
    OptionSpec(("--key",), "PEM client private key", "file"),
    OptionSpec(("--key-password",), "client private-key password", "text"),
    OptionSpec(("--servername",), "TLS server identity", "text"),
    OptionSpec(("--authority",), "HTTP/2 authority", "text"),
    OptionSpec(("--connect-timeout",), "connection timeout", "number"),
    OptionSpec(("-m", "--max-time"), "RPC deadline", "number"),
    OptionSpec(
        ("--max-recv-message-size",), "maximum response bytes", "number"
    ),
    OptionSpec(("--max-send-message-size",), "maximum request bytes", "number"),
    OptionSpec(("--compression",), "gRPC compression", "compression"),
    OptionSpec(("--keepalive-time",), "HTTP/2 keepalive interval", "number"),
    OptionSpec(("-A", "--user-agent"), "primary gRPC user agent", "text"),
    OptionSpec(("-H", "--header"), "shared metadata", "header", True),
    OptionSpec(
        ("--reflect-header",), "reflection-only metadata", "header", True
    ),
    OptionSpec(("--color",), "diagnostic color policy", "color"),
    OptionSpec(("--diagnostic-format",), "diagnostic encoding", "diagnostic"),
    OptionSpec(("-v", "--verbose"), "increase diagnostics", repeatable=True),
)

_INVOKE_OPTIONS = (
    OptionSpec(("--version",), "show the protodial version"),
    OptionSpec(("-d", "--data"), "request data or @file", "data"),
    OptionSpec(("--input-format",), "request encoding", "format"),
    OptionSpec(("--output-format",), "response encoding", "format"),
    OptionSpec(("--field",), "set a request field", "field", True),
    OptionSpec(
        ("--field:file",), "read a file into a bytes field", "field_file"
    ),
    OptionSpec(("--input-field",), "raw input bytes field", "input_field"),
    OptionSpec(("--output-field",), "raw output bytes field", "output_field"),
    OptionSpec(("--chunk-size",), "raw stream chunk size", "number"),
    OptionSpec(("--allow-unknown-fields",), "allow unknown ProtoJSON fields"),
    OptionSpec(("--emit-defaults",), "emit default-valued fields"),
    OptionSpec(("--max-input-buffer",), "maximum buffered input", "number"),
    OptionSpec(("--wait-for-ready",), "wait for an available server"),
    OptionSpec(("-i", "--include"), "show response headers"),
    OptionSpec(("--show-trailers",), "show response trailers"),
    OptionSpec(("--show-status",), "show final RPC status"),
    OptionSpec(("--format-error",), "decode structured gRPC errors"),
    OptionSpec(("--rpc-header",), "RPC-only metadata", "header", True),
    OptionSpec(("--method",), "service/method for a native target", "method"),
)

_LIST_OPTIONS = (
    OptionSpec(("--show-reflection",), "include reflection services"),
)
_DESCRIBE_OPTIONS = (
    OptionSpec(("--template",), "print sample request ProtoJSON"),
    OptionSpec(("--template-depth",), "maximum template depth", "number"),
)

VALUE_CHOICES = {
    "format": ("json", "text", "protobuf", "raw"),
    "compression": ("none", "gzip", "deflate"),
    "color": ("auto", "always", "never"),
    "diagnostic": ("human", "json"),
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


def _options(mode: str) -> tuple[OptionSpec, ...]:
    if mode == "list":
        return (*_SHARED_OPTIONS, *_LIST_OPTIONS)
    if mode == "describe":
        return (*_SHARED_OPTIONS, *_DESCRIBE_OPTIONS)
    return (*_SHARED_OPTIONS, *_INVOKE_OPTIONS)


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
