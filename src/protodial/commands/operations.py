"""Typed command operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Invoke:
    """Invoke one fully qualified RPC method."""

    method: str


@dataclass(frozen=True)
class ListSymbols:
    """List reflected or locally loaded services and methods."""

    service: str | None


@dataclass(frozen=True)
class Describe:
    """Describe one schema symbol."""

    symbol: str


Operation = Invoke | ListSymbols | Describe
