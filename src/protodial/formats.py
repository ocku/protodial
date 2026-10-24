"""Message serialization formats shared across CLI and RPC layers."""

from __future__ import annotations

from enum import Enum


class MessageFormat(str, Enum):
    """Supported request and response serialization formats."""

    JSON = "json"
    TEXT = "text"
    PROTOBUF = "protobuf"
    RAW = "raw"
