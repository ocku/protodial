"""Lazy loading of optional RPC runtime dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..errors import DEPENDENCY_MESSAGE, DependencyError

if TYPE_CHECKING:
    from ..schema.contracts import GrpcModule


@dataclass(frozen=True)
class Runtime:
    """Loaded gRPC runtime modules and protobuf error types."""

    grpc: GrpcModule
    protobuf_errors: tuple[type[Exception], ...]


def load_runtime() -> Runtime:
    """Load required runtime dependencies or raise a CLI error."""
    try:
        import grpc
        from google.protobuf.message import DecodeError, EncodeError
    except ImportError as error:
        raise DependencyError(DEPENDENCY_MESSAGE) from error
    return Runtime(
        grpc=cast("GrpcModule", grpc),
        protobuf_errors=(DecodeError, EncodeError),
    )
