"""Public facade and orchestration for tolerant shell completion."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from ..schema.contracts import DescriptorSource
from ._arguments import (
    VALUE_CHOICES as _VALUE_CHOICES,
)
from ._arguments import (
    Candidate,
)
from ._arguments import (
    OptionSpec as _Option,
)
from ._arguments import (
    Scan as _Scan,
)
from ._arguments import (
    filter_candidates as _filter,
)
from ._arguments import (
    option_candidates as _option_candidates,
)
from ._arguments import (
    scan as _scan,
)
from ._arguments import (
    unique_candidates as _unique,
)
from ._reflection import (
    OpenedReflectionSource as _OpenedReflectionSource,
)
from ._reflection import (
    reflection_source as _reflection_source,
)
from ._saved_protos import (
    SavedProtoSource as _SavedProtoSource,
)
from ._saved_protos import (
    local_sources as _local_sources,
)
from ._saved_protos import (
    saved_proto_files,
)
from ._schema_candidates import (
    field_candidates as _field_candidates,
)
from ._schema_candidates import (
    method_candidates as _methods,
)
from ._schema_candidates import (
    symbol_candidates as _symbols,
)

_MAX_DEPTH = 8
_MAX_SCAN_ENTRIES = 8192
_MAX_DIRECTORY_ENTRIES = 4096
_PATH_SCAN_SECONDS = 0.05
_CWD_SCAN_SECONDS = 0.05

__all__ = [
    "Candidate",
    "_OpenedReflectionSource",
    "_local_sources",
    "_methods",
    "_reflection_source",
    "_scan",
    "_symbols",
    "complete",
]


def _walk_files(root: Path, suffix: str | None) -> Iterable[Path]:
    if not root.is_dir():
        return
    scanned = 0
    deadline = time.monotonic() + _CWD_SCAN_SECONDS
    pending = [(root, 0)]
    while pending and scanned < _MAX_SCAN_ENTRIES:
        if time.monotonic() >= deadline:
            return
        directory, depth = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                scanned += 1
                if (
                    scanned > _MAX_SCAN_ENTRIES
                    or time.monotonic() >= deadline
                ):
                    return
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _MAX_DEPTH:
                            pending.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if suffix is None or entry.name.endswith(suffix):
                    yield Path(entry.path)


def _path_candidates(
    prefix: str,
    cwd: Path,
    *,
    suffix: str | None = None,
    directories_only: bool = False,
) -> list[Candidate]:
    expanded = Path(prefix).expanduser() if prefix else Path(".")
    parent_text = str(expanded.parent) if prefix else "."
    parent = (
        expanded.parent if expanded.is_absolute() else cwd / expanded.parent
    )
    needle = expanded.name if prefix else ""
    result: list[Candidate] = []
    deadline = time.monotonic() + _PATH_SCAN_SECONDS
    try:
        entries = os.scandir(parent)
    except OSError:
        return []
    with entries:
        for index, entry in enumerate(entries):
            if (
                index >= _MAX_DIRECTORY_ENTRIES
                or time.monotonic() >= deadline
            ):
                break
            if not entry.name.startswith(needle):
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            value = str(Path(parent_text) / entry.name)
            if parent_text == ".":
                value = entry.name
            if is_directory:
                result.append(
                    Candidate(f"{value}/", "directory", append_space=False)
                )
            elif (
                is_file
                and not directories_only
                and (suffix is None or entry.name.endswith(suffix))
            ):
                result.append(Candidate(value, "file"))
    return sorted(result, key=lambda candidate: candidate.value)


def _proto_candidates(
    prefix: str, scan: _Scan, environment: Mapping[str, str], cwd: Path
) -> list[Candidate]:
    result = _path_candidates(prefix, cwd, suffix=".proto")
    for path in _walk_files(cwd, ".proto"):
        value = str(path.relative_to(cwd))
        if value.startswith(prefix):
            result.append(Candidate(value, "proto in current directory"))
    result.extend(
        Candidate(path, "saved proto")
        for path in saved_proto_files(environment)
        if path.startswith(prefix)
    )
    return _unique(result)


def _sources(
    scan: _Scan,
    environment: Mapping[str, str],
    *,
    reflection: bool,
    union_saved_methods: bool = False,
    saved_detail_fallback: bool = False,
) -> tuple[list[DescriptorSource], _OpenedReflectionSource | None]:
    local_sources = _local_sources(scan, environment)
    explicit_sources = [
        source
        for source in local_sources
        if not isinstance(source, _SavedProtoSource)
    ]
    saved_sources = [
        source
        for source in local_sources
        if isinstance(source, _SavedProtoSource)
    ]
    has_explicit_local = bool(
        scan.values.get("--proto") or scan.values.get("--protoset")
    )
    use_reflection = reflection and (
        not has_explicit_local or "--use-reflection" in scan.flags
    )
    opened = _reflection_source(scan, environment) if use_reflection else None
    # Explicit inputs retain normal local/composite CLI precedence. A live
    # reflection source is next. Saved protos are only a detail fallback;
    # only method-name enumeration opts into a live+saved union below.
    sources = list(explicit_sources)
    if opened is not None:
        sources.append(opened.source)
    if not sources or union_saved_methods or saved_detail_fallback:
        sources.extend(saved_sources)
    return sources, opened



def _value_candidates(
    scan: _Scan,
    option: _Option,
    environment: Mapping[str, str],
    cwd: Path,
) -> list[Candidate]:
    prefix = scan.value_prefix
    assignment = ""
    if scan.current.startswith("-") and "=" in scan.current:
        assignment = scan.current.partition("=")[0] + "="
    kind = option.value_kind or ""
    if kind in _VALUE_CHOICES:
        return _filter(
            (
                Candidate(value, f"{kind} value")
                for value in _VALUE_CHOICES[kind]
            ),
            prefix,
            assignment=assignment,
        )
    if kind == "proto":
        values = _proto_candidates(prefix, scan, environment, cwd)
    elif kind == "directory":
        values = _path_candidates(prefix, cwd, directories_only=True)
    elif kind in {"file", "protoset"}:
        values = _path_candidates(prefix, cwd)
    elif kind == "data" and prefix.startswith("@"):
        values = [
            Candidate(
                f"@{item.value}",
                item.description,
                item.append_space,
            )
            for item in _path_candidates(prefix[1:], cwd)
        ]
    elif kind == "method":
        sources, opened = _sources(
            scan,
            environment,
            reflection=True,
            union_saved_methods=True,
        )
        try:
            values = _filter(_methods(sources, prefix), prefix)
        finally:
            if opened is not None:
                opened.close()
    elif kind in {"field", "field_file", "input_field", "output_field"}:
        sources, opened = _sources(
            scan,
            environment,
            reflection=True,
            saved_detail_fallback=True,
        )
        try:
            if kind == "field_file" and "=" in prefix:
                name, _, path_prefix = prefix.partition("=")
                known = _field_candidates(scan, kind, name, sources)
                if any(item.value == f"{name}=" for item in known):
                    values = [
                        Candidate(
                            f"{name}={item.value}",
                            item.description,
                            item.append_space,
                        )
                        for item in _path_candidates(path_prefix, cwd)
                    ]
                else:
                    values = []
            else:
                values = _field_candidates(scan, kind, prefix, sources)
        finally:
            if opened is not None:
                opened.close()
    else:
        values = []
    if assignment:
        return [
            Candidate(
                f"{assignment}{item.value}",
                item.description,
                item.append_space,
            )
            for item in values
        ]
    return values


def _completion_command(scan: _Scan) -> list[Candidate]:
    if not scan.positionals:
        if scan.current.startswith("-"):
            return _filter(
                (
                    Candidate("-h", "show completion help"),
                    Candidate("--help", "show completion help"),
                ),
                scan.current,
            )
        return _filter(
            (
                Candidate("bash", "print Bash completion"),
                Candidate("zsh", "print Zsh completion"),
                Candidate("install", "install shell completion"),
            ),
            scan.current,
        )
    if scan.positionals[0] != "install":
        return []
    if scan.current.startswith("--shell="):
        prefix = scan.current.removeprefix("--shell=")
        return [
            Candidate(f"--shell={shell}", f"{shell} shell")
            for shell in ("bash", "zsh")
            if shell.startswith(prefix)
        ]
    if scan.current.startswith("-"):
        return _filter(
            (
                Candidate("-h", "show install help"),
                Candidate("--help", "show install help"),
                Candidate("--shell", "shell to install for"),
                Candidate("--path", "destination file"),
                Candidate("--force", "replace existing file"),
            ),
            scan.current,
        )
    previous = scan.positionals[-1] if scan.positionals else ""
    if previous == "--shell":
        return _filter(
            (Candidate("bash", "Bash"), Candidate("zsh", "Zsh")),
            scan.current,
        )
    if previous == "--path":
        return _path_candidates(scan.current, Path.cwd())
    return []


def _method_target_candidates(
    scan: _Scan, environment: Mapping[str, str]
) -> list[Candidate]:
    current = scan.current
    if not current.startswith(("grpc://", "grpcs://")):
        return []
    try:
        parsed = urlsplit(current)
    except ValueError:
        return []
    if not parsed.netloc:
        return []
    base = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    method_prefix = parsed.path.lstrip("/")
    sources, opened = _sources(
        scan,
        environment,
        reflection=True,
        union_saved_methods=True,
    )
    try:
        return [
            Candidate(f"{base}/{item.value}", item.description)
            for item in _methods(sources, method_prefix)
            if item.value.startswith(method_prefix)
        ]
    finally:
        if opened is not None:
            opened.close()


def _discovery_url_candidates(
    scan: _Scan, environment: Mapping[str, str]
) -> list[Candidate]:
    current = scan.current
    if not current.startswith(("grpc://", "grpcs://")):
        return []
    try:
        parsed = urlsplit(current)
    except ValueError:
        return []
    if not parsed.netloc:
        return []
    base = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    prefix = parsed.path.lstrip("/")
    sources, opened = _sources(scan, environment, reflection=True)
    try:
        if scan.mode == "list":
            items: list[Candidate] = []
            for source in sources:
                try:
                    names: Sequence[str]
                    if isinstance(source, _SavedProtoSource):
                        saved_source = cast(
                            _SavedProtoSource, cast(object, source)
                        )
                        names = saved_source.list_services(prefix)
                    else:
                        names = source.list_services()
                    items.extend(Candidate(name, "service") for name in names)
                except Exception:
                    continue
            candidates = _unique(items)
        else:
            candidates = _symbols(sources, prefix)
        return [
            Candidate(f"{base}/{item.value}", item.description)
            for item in candidates
            if item.value.startswith(prefix)
        ]
    finally:
        if opened is not None:
            opened.close()


def _positional_candidates(
    scan: _Scan, environment: Mapping[str, str]
) -> list[Candidate]:
    if scan.mode == "invoke":
        return _method_target_candidates(scan, environment)
    if scan.current.startswith(("grpc://", "grpcs://")):
        return _discovery_url_candidates(scan, environment)
    if not scan.positionals:
        return []
    if scan.mode == "list" and len(scan.positionals) == 1:
        sources, opened = _sources(scan, environment, reflection=True)
        try:
            result: list[Candidate] = []
            for source in sources:
                try:
                    names: Sequence[str]
                    if isinstance(source, _SavedProtoSource):
                        saved_source = cast(
                            _SavedProtoSource, cast(object, source)
                        )
                        names = saved_source.list_services(scan.current)
                    else:
                        names = source.list_services()
                    result.extend(
                        Candidate(name, "service")
                        for name in names
                        if name.startswith(scan.current)
                    )
                except Exception:
                    continue
            return _unique(result)
        finally:
            if opened is not None:
                opened.close()
    if scan.mode == "describe" and len(scan.positionals) == 1:
        sources, opened = _sources(scan, environment, reflection=True)
        try:
            return _filter(_symbols(sources, scan.current), scan.current)
        finally:
            if opened is not None:
                opened.close()
    return []


def complete(
    words: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    cursor: int | None = None,
) -> list[Candidate]:
    """Complete a partial argv without requiring it to parse successfully."""
    scan = _scan(words, cursor)
    if scan.mode == "completion":
        return _completion_command(scan)
    if scan.value_option is not None:
        return _value_candidates(scan, scan.value_option, environment, cwd)
    if scan.current.startswith("-"):
        return _option_candidates(scan)
    if not words or (
        len(words) == 1 and not scan.current.startswith(("grpc://", "grpcs://"))
    ):
        commands = (
            Candidate("list", "list services or methods"),
            Candidate("describe", "describe a schema symbol"),
            Candidate("completion", "generate or install shell completion"),
        )
        combined = [*commands, *_option_candidates(scan)]
        return _filter(combined, scan.current)
    return _positional_candidates(scan, environment)
