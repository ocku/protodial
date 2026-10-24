"""Parse and validate scoped gRPC metadata."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .types import Metadata, MetadataEntry, MetadataValue

_VALID_KEY = re.compile(r"^[0-9a-z_.-]+$")
_VALID_NON_BINARY_VALUE = re.compile(r"^[\x20-\x7e]*$")


class MetadataError(ValueError):
    """Raised when command-line metadata is invalid."""


@dataclass(frozen=True)
class MetadataScopes:
    """Metadata split into shared, reflection-only, and RPC-only scopes."""

    shared: tuple[tuple[str, str | bytes], ...] = ()
    reflection_only: tuple[tuple[str, str | bytes], ...] = ()
    rpc_only: tuple[tuple[str, str | bytes], ...] = ()

    @property
    def reflection(self) -> Metadata:
        """Return metadata sent during schema reflection."""
        return [*self.shared, *self.reflection_only]

    @property
    def rpc(self) -> Metadata:
        """Return metadata sent during RPC invocation."""
        return [*self.shared, *self.rpc_only]


def validate_metadata_key(key: str) -> str:
    """Normalize and validate a gRPC metadata key."""

    normalized = key.lower()
    if not normalized:
        raise MetadataError("metadata key must not be empty")
    if not normalized.isascii() or _VALID_KEY.fullmatch(normalized) is None:
        raise MetadataError(
            f"invalid metadata key {key!r}; use only ASCII letters, digits, "
            "'.', '_', and '-'"
        )
    return normalized


def parse_metadata_entry(header: str) -> MetadataEntry:
    """Parse one ``key:value`` entry.

    One optional space or tab immediately following the colon is treated as the
    delimiter. All other value whitespace is preserved, including trailing
    space.
    """

    key_text, separator, value = header.partition(":")
    if not separator:
        raise MetadataError("metadata must have the form key:value")
    key = validate_metadata_key(key_text)
    if value.startswith((" ", "\t")):
        value = value[1:]

    if key.endswith("-bin"):
        try:
            decoded: MetadataValue = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise MetadataError(
                f"metadata {key!r} contains invalid base64"
            ) from None
    else:
        if _VALID_NON_BINARY_VALUE.fullmatch(value) is None:
            invalid_index = next(
                index
                for index, character in enumerate(value)
                if not " " <= character <= "~"
            )
            invalid = value[invalid_index]
            raise MetadataError(
                f"metadata {key!r} contains an invalid non-binary value "
                f"character at position {invalid_index}: U+{ord(invalid):04X}; "
                "gRPC metadata values must contain only printable ASCII "
                "characters (0x20-0x7E). Use a '-bin' "
                "key with a base64-encoded value for arbitrary bytes"
            )
        decoded = value
    return key, decoded


def parse_metadata(headers: Iterable[str]) -> Metadata:
    """Parse metadata in input order without collapsing duplicate keys."""

    return [parse_metadata_entry(header) for header in headers]


def parse_metadata_scopes(
    shared: Iterable[str] = (),
    reflection: Iterable[str] = (),
    rpc: Iterable[str] = (),
) -> MetadataScopes:
    """Parse all scopes; scope-specific entries follow shared entries."""

    return MetadataScopes(
        shared=tuple(parse_metadata(shared)),
        reflection_only=tuple(parse_metadata(reflection)),
        rpc_only=tuple(parse_metadata(rpc)),
    )
