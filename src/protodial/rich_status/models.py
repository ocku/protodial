"""Define decoded rich-status values."""

from __future__ import annotations

from dataclasses import dataclass

from google.protobuf.message import Message


@dataclass(frozen=True)
class DecodedStatusDetail:
    """One decoded or opaque google.rpc.Status detail."""

    type_url: str
    type_name: str
    payload: bytes
    message: Message | None = None
    warning: str | None = None

    @property
    def known(self) -> bool:
        """Return whether the detail payload was decoded."""
        return self.message is not None


@dataclass(frozen=True)
class RichStatus:
    """A decoded google.rpc.Status and its detail payloads."""

    code: int | None
    message: str
    details: tuple[DecodedStatusDetail, ...]
    warnings: tuple[str, ...] = ()
    raw_size: int = 0

    @property
    def decoded(self) -> bool:
        """Return whether the status payload was decoded."""
        return self.code is not None


DecodedStatus = RichStatus
StatusDetail = DecodedStatusDetail
