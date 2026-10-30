"""Bounded discovery of reusable proto files for shell completion."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from google.protobuf.descriptor_pool import DescriptorPool

from ..schema.contracts import DescriptorSource
from ..schema.paths import default_proto_paths
from ._arguments import Candidate, Scan, unique_candidates
from ._proto_syntax import (
    PACKAGE as _PACKAGE,
)
from ._proto_syntax import (
    RPC as _RPC,
)
from ._proto_syntax import (
    direct_blocks as _direct_blocks,
)
from ._proto_syntax import (
    direct_statements as _direct_statements,
)
from ._proto_syntax import (
    enum_symbols as _enum_symbols,
)
from ._proto_syntax import (
    extension_symbols as _extension_symbols,
)
from ._proto_syntax import (
    mask_comments_and_strings as _mask_comments_and_strings,
)
from ._proto_syntax import (
    message_symbols as _message_symbols,
)

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
            symbols.extend(_extension_symbols(body, package))
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
