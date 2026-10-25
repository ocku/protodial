"""Target and selected-method resolution shared by completion sources."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from ._arguments import Scan


def reflection_endpoint(scan: Scan) -> str | None:
    target: str | None = None
    if scan.mode in {"list", "describe"} and scan.positionals:
        target = scan.positionals[0]
    elif scan.mode == "invoke" and scan.positionals:
        target = scan.positionals[-1]
    if target is None and scan.current.startswith(("grpc://", "grpcs://")):
        target = scan.current
    if target is None:
        return None
    if target.startswith(("grpc://", "grpcs://")):
        try:
            parsed = urlsplit(target)
        except ValueError:
            return None
        if not parsed.netloc:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return target


def selected_method(scan: Scan) -> str | None:
    method = scan.last("--method")
    if method:
        return method
    target = reflection_endpoint(scan)
    if target is None:
        return None
    candidates = [*scan.positionals, scan.current]
    for value in reversed(candidates):
        if not value.startswith(("grpc://", "grpcs://")):
            continue
        try:
            path = urlsplit(value).path.lstrip("/")
        except ValueError:
            continue
        if path.count("/") == 1 and all(path.split("/")):
            return path
    return None


