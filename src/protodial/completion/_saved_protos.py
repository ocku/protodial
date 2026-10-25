"""Bounded discovery of reusable proto files for shell completion."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from google.protobuf.descriptor_pool import DescriptorPool

from ..schema.contracts import DescriptorSource
from ..schema.paths import default_proto_paths
from ._arguments import Candidate, Scan, unique_candidates

_MAX_DEPTH = 6
_MAX_ENTRIES = 4096
_MAX_FILES = 512
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_SCAN_SECONDS = 0.1
_COMPILE_SECONDS = 0.25
_MAX_PROTOSET_BYTES = 1024 * 1024
_MAX_PROTOSET_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_PROTOSETS = 16

_PACKAGE = re.compile(r"\bpackage\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;")
_BLOCK = re.compile(
    r"\b(?P<kind>service|message|enum|oneof|extend)\s+"
    r"(?P<name>[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\{"
)
_RPC = re.compile(
    r"\brpc\s+(?P<name>[A-Za-z_]\w*)\s*\(\s*"
    r"(?P<client>stream\s+)?[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\)"
    r"\s*returns\s*\(\s*(?P<server>stream\s+)?"
    r"[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\)"
)
_FIELD = re.compile(
    r"(?:^|;)\s*(?:(?:optional|required|repeated)\s+)?"
    r"(?:map\s*<[^;{}]+>|[.A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+"
    r"([A-Za-z_]\w*)\s*=\s*\d+\b",
    re.MULTILINE,
)
_ENUM_VALUE = re.compile(
    r"(?:^|;)\s*([A-Za-z_]\w*)\s*=\s*"
    r"-?(?:0[xX][0-9A-Fa-f]+|\d+)\b",
    re.MULTILINE,
)


@dataclass(slots=True)
class ScanBudget:
    """One intentionally small completion-time filesystem budget."""

    deadline: float = field(
        default_factory=lambda: time.monotonic() + _SCAN_SECONDS
    )
    entries: int = 0
    files: int = 0
    bytes: int = 0

    def active(self) -> bool:
        return time.monotonic() < self.deadline

    def can_scan(self) -> bool:
        return (
            self.active()
            and self.entries < _MAX_ENTRIES
            and self.files < _MAX_FILES
        )

    def claim_entry(self) -> bool:
        if not self.can_scan():
            return False
        self.entries += 1
        return True

    def claim_file(self) -> bool:
        if not self.can_scan():
            return False
        self.files += 1
        return True

    def claim_bytes(self, size: int) -> bool:
        if (
            not self.active()
            or size > _MAX_FILE_BYTES
            or self.bytes + size > _MAX_TOTAL_BYTES
        ):
            return False
        self.bytes += size
        return True


@dataclass(frozen=True, slots=True)
class ProtoEntry:
    """Names extracted from one saved proto without compiling it."""

    path: str
    services: tuple[str, ...]
    methods: tuple[Candidate, ...]
    symbols: tuple[Candidate, ...]


def saved_proto_files(
    environment: Mapping[str, str], budget: ScanBudget | None = None
) -> tuple[str, ...]:
    """Find a bounded set of ordinary, non-symlink saved proto files."""
    active = budget or ScanBudget()
    found: list[str] = []
    pending = [
        (Path(value).expanduser().absolute(), 0)
        for value in default_proto_paths(environment)
    ]
    while pending and active.can_scan():
        directory, depth = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not active.claim_entry():
                    break
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _MAX_DEPTH:
                            pending.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.name.endswith(".proto") or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                except OSError:
                    continue
                if not active.claim_file():
                    break
                found.append(entry.path)
    return tuple(sorted(found))


def _mask_comments_and_strings(text: str) -> str:
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


def _read_regular_proto(path: Path) -> str | None:
    """Read one bounded regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size > _MAX_FILE_BYTES
        ):
            return None
        with os.fdopen(descriptor, encoding="utf-8") as source:
            descriptor = None
            return source.read(_MAX_FILE_BYTES + 1)
    except (OSError, UnicodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _snapshot_protosets(
    paths: Sequence[str],
) -> tuple[tuple[str, ...], tempfile.TemporaryDirectory[str] | None] | None:
    if not paths:
        return (), None
    if len(paths) > _MAX_PROTOSETS:
        return None
    deadline = time.monotonic() + _SCAN_SECONDS
    directory = tempfile.TemporaryDirectory(
        prefix="protodial-completion-protoset-"
    )
    snapshots: list[str] = []
    total = 0
    try:
        for index, path in enumerate(paths):
            resolved = Path(path).expanduser().resolve(strict=True)
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(resolved, flags)
            try:
                details = os.fstat(descriptor)
                if (
                    time.monotonic() >= deadline
                    or not stat.S_ISREG(details.st_mode)
                    or details.st_size > _MAX_PROTOSET_BYTES
                ):
                    return None
                with os.fdopen(descriptor, "rb") as source:
                    descriptor = -1
                    data = source.read(_MAX_PROTOSET_BYTES + 1)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            total += len(data)
            if (
                len(data) > _MAX_PROTOSET_BYTES
                or total > _MAX_PROTOSET_TOTAL_BYTES
                or time.monotonic() >= deadline
            ):
                return None
            snapshot = Path(directory.name) / f"{index}.protoset"
            snapshot.write_bytes(data)
            snapshots.append(str(snapshot))
        return tuple(snapshots), directory
    except OSError:
        return None
    finally:
        if not snapshots or len(snapshots) != len(paths):
            directory.cleanup()


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


def _direct_statements(body: str) -> str:
    result = list(body)
    depth = 0
    for index, character in enumerate(body):
        if character == "{":
            depth += 1
        if depth and character != "\n":
            result[index] = " "
        if character == "}":
            depth = max(0, depth - 1)
    return "".join(result)


def _direct_blocks(text: str) -> Iterable[tuple[str, str, str]]:
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


def _enum_symbols(body: str, name: str) -> list[Candidate]:
    result = [Candidate(name, "enum")]
    scope, _, _ = name.rpartition(".")
    result.extend(
        Candidate(
            f"{scope}.{match.group(1)}" if scope else match.group(1),
            "enum value",
        )
        for match in _ENUM_VALUE.finditer(_direct_statements(body))
    )
    return result


def _message_symbols(body: str, name: str) -> list[Candidate]:
    result = [Candidate(name, "message")]
    result.extend(
        Candidate(f"{name}.{match.group(1)}", "field")
        for match in _FIELD.finditer(_direct_statements(body))
    )
    for kind, child_name, child_body in _direct_blocks(body):
        qualified = f"{name}.{child_name}"
        if kind == "message":
            result.extend(_message_symbols(child_body, qualified))
        elif kind == "enum":
            result.extend(_enum_symbols(child_body, qualified))
        elif kind == "oneof":
            result.append(Candidate(qualified, "oneof"))
            result.extend(
                Candidate(f"{name}.{match.group(1)}", "field")
                for match in _FIELD.finditer(_direct_statements(child_body))
            )
        elif kind == "extend":
            result.extend(
                Candidate(f"{name}.{match.group(1)}", "extension")
                for match in _FIELD.finditer(_direct_statements(child_body))
            )
    return result


def parse_proto(path: str, budget: ScanBudget) -> ProtoEntry | None:
    """Extract completion names from one small regular proto file."""
    text = _read_regular_proto(Path(path))
    if text is None or not budget.claim_bytes(len(text.encode("utf-8"))):
        return None
    text = _mask_comments_and_strings(text)
    package_match = _PACKAGE.search(_direct_statements(text))
    package = package_match.group(1) if package_match is not None else ""

    def qualify(name: str) -> str:
        return f"{package}.{name}" if package else name

    services: list[str] = []
    methods: list[Candidate] = []
    symbols: list[Candidate] = []
    for kind, block_name, body in _direct_blocks(text):
        name = qualify(block_name)
        if kind == "service":
            services.append(name)
            symbols.append(Candidate(name, "service"))
            for rpc in _RPC.finditer(_direct_statements(body)):
                description = (
                    ("stream" if rpc.group("client") else "unary")
                    + " request, "
                    + ("stream" if rpc.group("server") else "unary")
                    + " response"
                )
                method = f"{name}/{rpc.group('name')}"
                methods.append(Candidate(method, description))
                symbols.append(Candidate(method, "RPC method"))
        elif kind == "message":
            symbols.extend(_message_symbols(body, name))
        elif kind == "enum":
            symbols.extend(_enum_symbols(body, name))
        elif kind == "extend":
            symbols.extend(
                Candidate(qualify(match.group(1)), "extension")
                for match in _FIELD.finditer(_direct_statements(body))
            )
    return ProtoEntry(
        path,
        tuple(dict.fromkeys(services)),
        tuple(unique_candidates(methods)),
        tuple(unique_candidates(symbols)),
    )


class SavedProtoSource:
    """Descriptor source backed by a small in-memory saved-proto scan."""

    def __init__(
        self,
        entries: Sequence[ProtoEntry],
        loader: Callable[
            [Sequence[str], Sequence[str]], DescriptorSource | None
        ],
    ) -> None:
        self._entries = tuple(entries)
        self._loader = loader
        self._compiled: dict[str, DescriptorSource | None] = {}
        self._pool = DescriptorPool()

    @property
    def pool(self) -> DescriptorPool:
        return self._pool

    def list_services(self, prefix: str = "") -> Sequence[str]:
        return tuple(
            sorted(
                service
                for entry in self._entries
                for service in entry.services
                if service.startswith(prefix)
            )
        )

    def method_candidates(self, prefix: str = "") -> list[Candidate]:
        return unique_candidates(
            candidate
            for entry in self._entries
            for candidate in entry.methods
            if candidate.value.startswith(prefix)
        )

    def symbol_candidates(self, prefix: str = "") -> list[Candidate]:
        return unique_candidates(
            candidate
            for entry in self._entries
            for candidate in entry.symbols
            if candidate.value.startswith(prefix)
        )

    def find_symbol(self, name: str) -> object:
        normalized = name.strip().lstrip("./")
        for entry in self._entries:
            if not any(
                candidate.value == normalized
                for candidate in (*entry.methods, *entry.symbols)
            ):
                continue
            if entry.path not in self._compiled:
                self._compiled[entry.path] = self._loader((entry.path,), ())
            source = self._compiled[entry.path]
            if source is None:
                continue
            try:
                return source.find_symbol(normalized)
            except Exception:
                continue
        raise KeyError(name)


def local_sources(
    scan: Scan, environment: Mapping[str, str]
) -> list[DescriptorSource]:
    """Load explicit schemas and one bounded automatic saved-proto scan."""
    from ..commands.models import Command
    from ..schema.local import local_descriptor_source

    roots = tuple(
        dict.fromkeys(
            (
                *default_proto_paths(environment),
                *scan.values.get("--proto-path", ()),
            )
        )
    )

    def load(
        protos: Sequence[str], protosets: Sequence[str]
    ) -> DescriptorSource | None:
        if not protos and not protosets:
            return None
        saved_protosets = _snapshot_protosets(protosets)
        if saved_protosets is None:
            return None
        snapshot_paths, snapshot_owner = saved_protosets
        command = cast(
            Command,
            cast(
                object,
                SimpleNamespace(
                    schema=SimpleNamespace(
                        proto=tuple(protos),
                        proto_path=roots,
                        protoset=snapshot_paths,
                    ),
                    _completion_compile_timeout=_COMPILE_SECONDS,
                ),
            ),
        )
        try:
            source = local_descriptor_source(command)
            if snapshot_owner is not None:
                setattr(
                    source,
                    "_protodial_completion_snapshot",
                    snapshot_owner,
                )
            return source
        except Exception:
            if snapshot_owner is not None:
                snapshot_owner.cleanup()
            return None

    sources: list[DescriptorSource] = []
    explicit = load(
        scan.values.get("--proto", ()), scan.values.get("--protoset", ())
    )
    if explicit is not None:
        sources.append(explicit)

    budget = ScanBudget()
    entries = tuple(
        parsed
        for path in saved_proto_files(environment, budget)
        if budget.active() and (parsed := parse_proto(path, budget)) is not None
    )
    if entries:
        sources.append(cast(DescriptorSource, SavedProtoSource(entries, load)))
    return sources
