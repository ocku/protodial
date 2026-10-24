"""Service listing and symbol description operations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol

from ..commands.models import Command
from ..errors import CliError, StdoutBrokenPipe

if TYPE_CHECKING:
    from ..schema.contracts import DescriptorSource, GrpcModule


class DescriptorSourceOpener(Protocol):
    """Open a configured descriptor source context."""

    def __call__(
        self, command: Command, grpc: GrpcModule
    ) -> AbstractContextManager[DescriptorSource]:
        """Return a descriptor source context for a command."""
        ...


def _print_stdout(value: object) -> None:
    """Write one discovery record and identify stdout pipe closure."""
    try:
        print(value)
    except BrokenPipeError as error:
        raise StdoutBrokenPipe from error


def open_descriptor_source(
    command: Command, grpc: GrpcModule
) -> AbstractContextManager[DescriptorSource]:
    """Open the descriptor source configured by a command."""
    from ..schema.context import open_descriptor_source as open_source

    return open_source(command, grpc)


def list_symbols(
    command: Command,
    grpc: GrpcModule,
    *,
    source_opener: DescriptorSourceOpener = open_descriptor_source,
) -> None:
    """List visible services or methods from the descriptor source."""
    from google.protobuf.descriptor import ServiceDescriptor

    from ..commands.operations import ListSymbols
    from ..schema.reflection import REFLECTION_SERVICES

    operation = command.operation
    if not isinstance(operation, ListSymbols):
        raise TypeError("list_symbols requires a ListSymbols operation")
    with source_opener(command, grpc) as source:
        if operation.service is None:
            for name in source.list_services():
                if command.show_reflection or name not in REFLECTION_SERVICES:
                    _print_stdout(name)
            return
        service = source.find_symbol(operation.service)
        if not isinstance(service, ServiceDescriptor):
            raise CliError(f"{operation.service!r} does not name a service")
        for method in service.methods:
            _print_stdout(f"{service.full_name}/{method.name}")


def describe(
    command: Command,
    grpc: GrpcModule,
    *,
    source_opener: DescriptorSourceOpener = open_descriptor_source,
) -> None:
    """Print a descriptor or an example message template."""
    from google.protobuf.descriptor import Descriptor, MethodDescriptor

    from ..commands.operations import Describe
    from ..presentation.descriptor import render_descriptor
    from ..presentation.templates import message_template

    operation = command.operation
    if not isinstance(operation, Describe):
        raise TypeError("describe requires a Describe operation")
    with source_opener(command, grpc) as source:
        symbol = source.find_symbol(operation.symbol)
        if command.get_template:
            if isinstance(symbol, MethodDescriptor):
                message = symbol.input_type
            elif isinstance(symbol, Descriptor):
                message = symbol
            else:
                raise CliError("--template requires a message or method symbol")
            _print_stdout(
                message_template(message, max_depth=command.template_depth)
            )
        else:
            _print_stdout(render_descriptor(symbol))
