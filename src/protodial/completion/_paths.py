"""Bounded filesystem discovery for path and proto completion."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path

from ._arguments import Candidate

_MAX_DEPTH = 8
_MAX_SCAN_ENTRIES = 8192
_MAX_DIRECTORY_ENTRIES = 4096
_PATH_SCAN_SECONDS = 0.05
_CWD_SCAN_SECONDS = 0.05


def walk_files(root: Path, suffix: str | None) -> Iterable[Path]:
    """Yield matching regular files within a bounded directory walk."""
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
                if scanned > _MAX_SCAN_ENTRIES or time.monotonic() >= deadline:
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


def path_candidates(
    prefix: str,
    cwd: Path,
    *,
    suffix: str | None = None,
    directories_only: bool = False,
) -> list[Candidate]:
    """Find bounded file or directory matches for a shell path prefix."""
    expanded = Path(prefix).expanduser() if prefix else Path(".")
    separators = tuple(
        separator for separator in (os.sep, os.altsep) if separator
    )
    # Path removes trailing separators, but they mean the user has entered
    # this directory and is completing its children rather than its name.
    inside_directory = not prefix or prefix.endswith(separators)
    directory = expanded if inside_directory else expanded.parent
    parent_text = str(directory)
    parent = directory if directory.is_absolute() else cwd / directory
    needle = "" if inside_directory else expanded.name
    result: list[Candidate] = []
    deadline = time.monotonic() + _PATH_SCAN_SECONDS
    try:
        entries = os.scandir(parent)
    except OSError:
        return []
    with entries:
        for index, entry in enumerate(entries):
            if index >= _MAX_DIRECTORY_ENTRIES or time.monotonic() >= deadline:
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
