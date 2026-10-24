"""Request metadata types, validation, and parsing."""

from .parsing import (
    MetadataError,
    MetadataScopes,
    parse_metadata,
    parse_metadata_entry,
    parse_metadata_scopes,
    validate_metadata_key,
)
from .types import Metadata, MetadataEntry, MetadataIterable, MetadataValue

__all__ = [
    "Metadata",
    "MetadataEntry",
    "MetadataError",
    "MetadataIterable",
    "MetadataScopes",
    "MetadataValue",
    "parse_metadata",
    "parse_metadata_entry",
    "parse_metadata_scopes",
    "validate_metadata_key",
]
