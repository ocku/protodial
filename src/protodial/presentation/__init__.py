"""Rendering and presentation helpers."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .rpc import (
    NamedStatus,
    RpcError,
    RpcSummary,
    render_metadata,
    render_rpc_diagnostics,
    render_rpc_error,
)

if TYPE_CHECKING:
    from .descriptor import DescriptorLike, render_descriptor
    from .templates import JSONScalar, JSONValue, message_template

__all__ = [
    "DescriptorLike",
    "JSONScalar",
    "JSONValue",
    "NamedStatus",
    "RpcError",
    "RpcSummary",
    "message_template",
    "render_descriptor",
    "render_metadata",
    "render_rpc_diagnostics",
    "render_rpc_error",
]

_LAZY_MODULES = {
    "DescriptorLike": "descriptor",
    "render_descriptor": "descriptor",
    "JSONScalar": "templates",
    "JSONValue": "templates",
    "message_template": "templates",
}


def __getattr__(name: str) -> object:
    """Load protobuf-backed helpers only after runtime validation."""
    module_name = _LAZY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
