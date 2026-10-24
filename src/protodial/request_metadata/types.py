"""Define shared gRPC metadata types."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeAlias

MetadataValue: TypeAlias = str | bytes
MetadataEntry: TypeAlias = tuple[str, MetadataValue]
Metadata: TypeAlias = list[MetadataEntry]
MetadataIterable: TypeAlias = Iterable[MetadataEntry]
