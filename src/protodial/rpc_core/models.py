"""RPC result and failure models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..request_metadata.types import Metadata

if TYPE_CHECKING:
    from google.protobuf.descriptor_pool import DescriptorPool

    from ..rich_status.models import RichStatus


@dataclass(frozen=True)
class RpcSummary:
    """Metadata, status, counts, and timing for one RPC."""

    initial_metadata: Metadata
    trailing_metadata: Metadata
    code: object
    details: str
    sent_messages: int
    received_messages: int
    elapsed_seconds: float


@dataclass
class RpcFailure(Exception):
    """An RPC failure with its collected summary and rich status."""

    error: Exception
    summary: RpcSummary
    status_rendered: bool
    rich_status: RichStatus | None = None
    descriptor_pool: DescriptorPool | None = None
    request_type: str | None = None
    method: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, str(self.error))


@dataclass
class MessageCounter:
    """A mutable message count shared across call helpers."""

    value: int = 0
