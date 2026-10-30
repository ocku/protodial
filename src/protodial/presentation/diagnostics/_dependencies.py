"""Translate unavailable declared dependencies into actionable errors."""

from __future__ import annotations

from ...errors import (
    DependencyError,
)

_DECLARED_DEPENDENCIES = {
    "cryptography": "cryptography",
    "grpc": "grpcio",
    "grpc_reflection": "grpcio-reflection",
    "grpc_tools": "grpcio-tools",
    "google.protobuf": "protobuf",
    "google.rpc": "googleapis-common-protos",
}


def missing_dependency(error: BaseException) -> DependencyError | None:
    """Translate a missing declared distribution to an actionable error."""
    if not isinstance(error, ImportError):
        return None
    missing = getattr(error, "name", None)
    if not isinstance(missing, str):
        return None
    distribution = next(
        (
            package
            for module, package in _DECLARED_DEPENDENCIES.items()
            if missing == module or missing.startswith(module + ".")
        ),
        None,
    )
    if distribution is None:
        return None
    return DependencyError(
        f"required dependency `{distribution}` is unavailable; reinstall with "
        f"`python3 -m pip install {distribution}`"
    )
