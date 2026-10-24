"""Metadata and request-input helpers."""

from __future__ import annotations

import contextlib
import io
import os
import sys
from collections.abc import Generator, Iterable
from typing import TYPE_CHECKING, BinaryIO

from ..errors import CliError, LocalFileError
from ..request_metadata.types import Metadata

if TYPE_CHECKING:
    from google.protobuf.descriptor import Descriptor


def parse_metadata(headers: Iterable[str]) -> Metadata:
    """Translate metadata parser errors into user-facing CLI errors."""
    from ..request_metadata.parsing import MetadataError
    from ..request_metadata.parsing import parse_metadata as parse

    try:
        return parse(headers)
    except MetadataError as error:
        raise CliError(str(error)) from error


def select_bytes_field(
    descriptor: Descriptor, name: str | None, option: str
) -> str:
    """Select one singular bytes field for raw message I/O."""
    from google.protobuf.descriptor import FieldDescriptor

    fields = [
        field
        for field in descriptor.fields
        if field.type == FieldDescriptor.TYPE_BYTES and not field.is_repeated
    ]
    if name:
        fields = [
            field for field in fields if name in (field.name, field.json_name)
        ]
    if len(fields) != 1:
        raise CliError(
            f"{descriptor.full_name}: raw mode requires one singular bytes "
            f"field; select it with {option}"
        )
    return fields[0].name


@contextlib.contextmanager
def open_input(
    data: str | None,
    *,
    argument: str = "--data",
    option_strings: tuple[str, ...] = ("-d", "--data"),
    option_value: str | None = None,
) -> Generator[BinaryIO]:
    """Open literal, file, or standard-input request data."""
    if data is None:
        yield io.BytesIO()
    elif data in ("-", "@-"):
        stdin = sys.stdin.buffer
        try:
            descriptor = stdin.fileno()
        except (AttributeError, io.UnsupportedOperation, OSError):
            yield stdin
        else:
            # grpcio consumes client streams on a daemon thread. Use an
            # unbuffered duplicate so a terminal RPC cannot leave that thread
            # holding BufferedReader's lock during interpreter shutdown.
            with os.fdopen(os.dup(descriptor), "rb", buffering=0) as source:
                yield source
    elif data.startswith("@"):
        path = data[1:]
        try:
            source = open(path, "rb")
        except OSError as error:
            description = (
                "field file"
                if argument == "--field:file"
                else "request data file"
            )
            raise LocalFileError(
                description,
                path,
                argument,
                error,
                occurrence=-1,
                option_strings=option_strings,
                option_value=option_value if option_value is not None else data,
            ) from error
        try:
            yield source
        finally:
            source.close()
    else:
        yield io.BytesIO(data.encode("utf-8"))


def read_exact(
    source: BinaryIO, size: int, *, allow_eof: bool = False
) -> bytes | None:
    """Read exactly a requested byte count with optional clean EOF."""
    data = bytearray()
    while len(data) < size:
        chunk = source.read(size - len(data))
        if not chunk:
            if allow_eof and not data:
                return None
            raise CliError("truncated length-prefixed protobuf input")
        data.extend(chunk)
    return bytes(data)
