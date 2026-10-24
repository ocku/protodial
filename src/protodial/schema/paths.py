"""Default filesystem locations for reusable protobuf schemas."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..errors import CliError

_XDG_PROTO_DIRECTORY_PARTS = ("protodial", "protos")
_HOME_PROTO_DIRECTORY_PARTS = (".local", "share", *_XDG_PROTO_DIRECTORY_PARTS)


def default_proto_paths(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return the XDG data directory used for reusable proto files.

    This function only derives a path. Parsing and read-only commands must not
    create it as a side effect.
    """

    values = os.environ if environment is None else environment
    data_home = values.get("XDG_DATA_HOME")
    if data_home:
        return (
            str(
                Path(data_home)
                .expanduser()
                .joinpath(*_XDG_PROTO_DIRECTORY_PARTS)
            ),
        )
    home = values.get("HOME") or str(Path.home())
    return (
        str(Path(home).expanduser().joinpath(*_HOME_PROTO_DIRECTORY_PARTS)),
    )


def ensure_default_proto_paths(
    paths: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Create default proto roots and return their normalized paths."""

    resolved = tuple(paths) if paths is not None else default_proto_paths()
    for value in resolved:
        path = Path(value)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CliError(
                f"cannot create default proto directory {path}: {error}"
            ) from error
    return resolved
