"""Parse and validate scoped gRPC metadata."""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .types import Metadata, MetadataEntry, MetadataValue

_VALID_KEY = re.compile(r"^[0-9a-z_.-]+$")
_VALID_NON_BINARY_VALUE = re.compile(r"^[\x20-\x7e]*$")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def expand_metadata_value(
    value: str, environ: Mapping[str, str] | None = None
) -> str:
    """Expand $NAME/${NAME}; $$ is a literal dollar and no shell is invoked."""

    environment = os.environ if environ is None else environ
    expanded: list[str] = []
    position = 0
    while position < len(value):
        dollar = value.find("$", position)
        if dollar < 0:
            expanded.append(value[position:])
            break
        expanded.append(value[position:dollar])
        if dollar + 1 >= len(value):
            expanded.append("$")
            break

        following = value[dollar + 1]
        if following == "$":
            expanded.append("$")
            position = dollar + 2
            continue
        if following == "{":
            closing = value.find("}", dollar + 2)
            if closing < 0:
                raise MetadataError(
                    "invalid environment expansion: unclosed '${'"
                )
            name = value[dollar + 2 : closing]
            if _ENVIRONMENT_NAME.fullmatch(name) is None:
                raise MetadataError(
                    f"invalid environment variable name {name!r}"
                )
            position = closing + 1
        else:
            match = _ENVIRONMENT_NAME.match(value, dollar + 1)
            if match is None:
                expanded.append("$")
                position = dollar + 1
                continue
            name = match.group()
            position = match.end()

        try:
            expanded.append(environment[name])
        except KeyError:
            raise MetadataError(
                f"environment variable {name!r} is not set"
            ) from None
    return "".join(expanded)


def parse_metadata_entry(
    header: str,
    *,
    expand: bool = False,
    environ: Mapping[str, str] | None = None,
) -> MetadataEntry:
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
    if expand:
        value = expand_metadata_value(value, environ)

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


def parse_metadata(
    headers: Iterable[str],
    *,
    expand: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Metadata:
    """Parse metadata in input order without collapsing duplicate keys."""

    return [
        parse_metadata_entry(header, expand=expand, environ=environ)
        for header in headers
    ]


def parse_metadata_scopes(
    shared: Iterable[str] = (),
    reflection: Iterable[str] = (),
    rpc: Iterable[str] = (),
    *,
    expand: bool = False,
    environ: Mapping[str, str] | None = None,
) -> MetadataScopes:
    """Parse all scopes; scope-specific entries follow shared entries."""

    return MetadataScopes(
        shared=tuple(parse_metadata(shared, expand=expand, environ=environ)),
        reflection_only=tuple(
            parse_metadata(reflection, expand=expand, environ=environ)
        ),
        rpc_only=tuple(parse_metadata(rpc, expand=expand, environ=environ)),
    )
