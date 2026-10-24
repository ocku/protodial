"""Index protobuf files and resolve descriptor symbols."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from typing import TypeGuard

from google.protobuf import descriptor_database, descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import (
    Descriptor,
    EnumDescriptor,
    FileDescriptor,
    MethodDescriptor,
)

from ..errors import CliError
from .contracts import DescriptorSymbol


class DescriptorBuildError(Exception):
    """A registered file cannot be built into a protobuf descriptor."""

    def __init__(self, file_name: str, error: BaseException) -> None:
        super().__init__(str(error))
        self.file_name = file_name
        self.original = error


class FileDescriptorRegistry:
    """Track descriptor files and reject ambiguous import identities."""

    def __init__(self) -> None:
        self.files: dict[str, descriptor_pb2.FileDescriptorProto] = {}
        self.origins: dict[str, str] = {}

    def add(
        self, file_proto: descriptor_pb2.FileDescriptorProto, origin: str
    ) -> bool:
        """Register a file unless an identical file is already present."""
        existing = self.files.get(file_proto.name)
        if existing is not None:
            if _semantic_file_bytes(existing) != _semantic_file_bytes(
                file_proto
            ):
                raise CliError(
                    f"conflicting descriptor {file_proto.name!r} from "
                    f"{self.origins[file_proto.name]} and {origin}"
                )
            return False
        self.files[file_proto.name] = file_proto
        self.origins[file_proto.name] = origin
        return True


class _DescriptorIndexBuilder:
    """Traverse descriptor files once and collect every named symbol."""

    def __init__(
        self, file_protos: dict[str, descriptor_pb2.FileDescriptorProto]
    ) -> None:
        self.symbols: dict[str, DescriptorSymbol] = {}
        self._file_protos = file_protos
        self._visited_files: set[str] = set()

    def add(self, symbol: DescriptorSymbol) -> None:
        full_name = getattr(symbol, "full_name", None)
        if full_name:
            self.symbols.setdefault(full_name, symbol)

    def has_visited_file(self, file_name: str) -> bool:
        """Return whether a file was already traversed."""
        return file_name in self._visited_files

    def visit_enum(self, enum: EnumDescriptor) -> None:
        self.add(enum)
        for value in enum.values:
            self.add(value)

    def visit_message(self, message: Descriptor) -> None:
        self.add(message)
        for symbol in (
            *message.fields,
            *message.oneofs,
            *message.extensions,
        ):
            self.add(symbol)
        for enum in message.enum_types:
            self.visit_enum(enum)
        for nested in message.nested_types:
            if not nested.GetOptions().map_entry:
                self.visit_message(nested)

    def visit_file(self, file_descriptor: FileDescriptor) -> None:
        if file_descriptor.name in self._visited_files:
            return
        self._visited_files.add(file_descriptor.name)
        _register_source_proto(file_descriptor, self._file_protos)

        for dependency in file_descriptor.dependencies:
            self.visit_file(dependency)
        for service in file_descriptor.services_by_name.values():
            self.add(service)
            for method in service.methods:
                self.add(method)
        for message in file_descriptor.message_types_by_name.values():
            self.visit_message(message)
        for enum in file_descriptor.enum_types_by_name.values():
            self.visit_enum(enum)
        for extension in file_descriptor.extensions_by_name.values():
            self.add(extension)


class IndexedDescriptorSource:
    """Descriptor source with deterministic, bounded symbol resolution."""

    def __init__(
        self,
        pool: descriptor_pool.DescriptorPool,
        service_names: Sequence[str],
        file_protos: dict[str, descriptor_pb2.FileDescriptorProto]
        | None = None,
    ) -> None:
        self._pool = pool
        self._service_names = tuple(sorted(dict.fromkeys(service_names)))
        self._symbols: dict[str, DescriptorSymbol] | None = None
        self._indexed_file_names: frozenset[str] | None = None
        self._file_protos = file_protos if file_protos is not None else {}
        self.registry: FileDescriptorRegistry | None = None

        from .source_protos import register_pool_file_protos

        register_pool_file_protos(self._pool, self._file_protos)

    @property
    def file_protos(self) -> dict[str, descriptor_pb2.FileDescriptorProto]:
        """Return source file protos indexed by import name."""
        return self._file_protos

    @property
    def pool(self) -> descriptor_pool.DescriptorPool:
        """Return the indexed descriptor pool."""
        return self._pool

    def list_services(self) -> Sequence[str]:
        """Return sorted, unique service names."""
        return self._service_names

    def _build_index(self) -> dict[str, DescriptorSymbol]:
        file_names = frozenset(self._file_protos)
        if (
            self._symbols is not None
            and self._indexed_file_names == file_names
        ):
            return self._symbols

        builder = _DescriptorIndexBuilder(self._file_protos)

        # Reflection sources fetch at most one exact descriptor for each
        # advertised service. Local sources additionally index every supplied
        # file, including valid declarations that are not reachable from a
        # service or dependency.
        for service_name in self._service_names:
            try:
                service = self._pool.FindServiceByName(service_name)
            except KeyError as error:
                raise CliError(
                    f"reflection advertised service {service_name!r} but did "
                    "not describe it"
                ) from error
            builder.visit_file(service.file)
        # A lazy database request may return more files than the one the pool
        # asked for. Keep consuming the shared mapping until every file added
        # during this build has been traversed too.
        while pending := tuple(
            file_name
            for file_name in self._file_protos
            if not builder.has_visited_file(file_name)
        ):
            for file_name in pending:
                builder.visit_file(self._pool.FindFileByName(file_name))

        self._symbols = builder.symbols
        self._indexed_file_names = frozenset(self._file_protos)
        return builder.symbols

    def find_symbol(self, name: str) -> DescriptorSymbol:
        """Resolve a symbol with exact and unambiguous suffix matching."""
        normalized = name.strip().lstrip("./")
        if not normalized:
            raise CliError("symbol name must not be empty")

        # Invocation normally supplies a fully qualified service/method name.
        # Ask the pool for that exact descriptor before walking the advertised
        # service graph. A reflection-backed pool can satisfy this with one
        # symbol request, so an unrelated service with a broken descriptor
        # cannot prevent the call.
        exact = self._find_exact(normalized)
        if exact is not None:
            return exact

        symbols = self._build_index()

        exact = symbols.get(normalized)
        if exact is not None:
            return exact

        exact, symbols = self._discover_unlinked_exact(normalized, symbols)
        if exact is not None:
            return exact

        matches = _matching_symbols(normalized, symbols)
        matches.sort(key=_symbol_full_name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            choices = ", ".join(_symbol_full_name(symbol) for symbol in matches)
            raise CliError(
                f"ambiguous symbol {name!r}; choose one of: {choices}"
            )

        nearby = difflib.get_close_matches(
            normalized, sorted(symbols), n=5, cutoff=0.45
        )
        suggestion = f"; nearby: {', '.join(nearby)}" if nearby else ""
        raise CliError(f"symbol {name!r} not found{suggestion}")

    def _discover_unlinked_exact(
        self,
        name: str,
        symbols: dict[str, DescriptorSymbol],
    ) -> tuple[DescriptorSymbol | None, dict[str, DescriptorSymbol]]:
        """Load an exact reflected declaration outside the service graph."""
        should_query = (
            "." in name
            and "/" not in name
            and not any(
                _component_suffix(full_name, name) for full_name in symbols
            )
        )
        if not should_query:
            return None, symbols
        try:
            self._pool.FindFileContainingSymbol(name)
        except KeyError:
            pass
        refreshed = self._build_index()
        return refreshed.get(name), refreshed

    def _find_exact(self, name: str) -> DescriptorSymbol | None:
        service_name, separator, method_name = name.rpartition("/")
        if separator:
            if not service_name or not method_name or "/" in service_name:
                return None
            dotted_name = f"{service_name}.{method_name}"
            lookup_name = service_name
        else:
            # Unqualified names are suffix searches. Querying reflection for
            # each one would turn a local ambiguity check into an unnecessary
            # RPC.
            if "." not in name:
                return None
            dotted_name = name
            container_name, _, _ = name.rpartition(".")
            lookup_name = (
                container_name
                if container_name in self._service_names
                else name
            )

        try:
            file_descriptor = self._pool.FindFileContainingSymbol(lookup_name)
        except KeyError:
            return None
        _register_source_file_tree(file_descriptor, self._file_protos)
        return _find_symbol_in_file(file_descriptor, dotted_name)


def _register_source_proto(
    file_descriptor: FileDescriptor,
    file_protos: dict[str, descriptor_pb2.FileDescriptorProto],
) -> None:
    source_proto = file_protos.get(file_descriptor.name)
    if source_proto is None:
        return
    from .source_protos import register_file_proto

    register_file_proto(file_descriptor, source_proto)


def _register_source_file_tree(
    file_descriptor: FileDescriptor,
    file_protos: dict[str, descriptor_pb2.FileDescriptorProto],
) -> None:
    """Associate a loaded file and dependencies with lossless source protos."""
    visited: set[str] = set()

    def visit(current: FileDescriptor) -> None:
        if current.name in visited:
            return
        visited.add(current.name)
        _register_source_proto(current, file_protos)
        for dependency in current.dependencies:
            visit(dependency)

    visit(file_descriptor)


def _find_symbol_in_file(
    file_descriptor: FileDescriptor, full_name: str
) -> DescriptorSymbol | None:
    """Find one exact symbol without probing the pool with incorrect types."""
    return _ExactSymbolFinder(full_name).visit_file(file_descriptor)


class _ExactSymbolFinder:
    def __init__(self, full_name: str) -> None:
        self.full_name = full_name

    def visit_enum(self, enum: EnumDescriptor) -> DescriptorSymbol | None:
        if enum.full_name == self.full_name:
            return enum
        return next(
            (
                value
                for value in enum.values
                if getattr(value, "full_name", None) == self.full_name
            ),
            None,
        )

    def visit_message(self, message: Descriptor) -> DescriptorSymbol | None:
        if message.full_name == self.full_name:
            return message
        direct_symbols = (*message.fields, *message.oneofs, *message.extensions)
        direct = next(
            (
                symbol
                for symbol in direct_symbols
                if symbol.full_name == self.full_name
            ),
            None,
        )
        if direct is not None:
            return direct
        for enum in message.enum_types:
            found = self.visit_enum(enum)
            if found is not None:
                return found
        for nested in message.nested_types:
            found = self.visit_message(nested)
            if found is not None:
                return found
        return None

    def visit_file(
        self, file_descriptor: FileDescriptor
    ) -> DescriptorSymbol | None:
        for service in file_descriptor.services_by_name.values():
            if service.full_name == self.full_name:
                return service
            method = next(
                (
                    method
                    for method in service.methods
                    if method.full_name == self.full_name
                ),
                None,
            )
            if method is not None:
                return method
        for message in file_descriptor.message_types_by_name.values():
            found = self.visit_message(message)
            if found is not None:
                return found
        for enum in file_descriptor.enum_types_by_name.values():
            found = self.visit_enum(enum)
            if found is not None:
                return found
        return next(
            (
                extension
                for extension in file_descriptor.extensions_by_name.values()
                if extension.full_name == self.full_name
            ),
            None,
        )


def _matching_symbols(
    name: str, symbols: dict[str, DescriptorSymbol]
) -> list[DescriptorSymbol]:
    """Return unique symbols matching dotted suffix or Service/Method syntax."""
    service_name, separator, method_name = name.rpartition("/")
    if separator:
        matches = (
            symbol
            for symbol in symbols.values()
            if _is_method(symbol)
            and symbol.name == method_name
            and _component_suffix(
                symbol.containing_service.full_name, service_name
            )
        )
    else:
        matches = (
            symbol
            for full_name, symbol in symbols.items()
            if _component_suffix(full_name, name)
        )
    return list(
        {_symbol_full_name(symbol): symbol for symbol in matches}.values()
    )


def _semantic_file_bytes(
    file_proto: descriptor_pb2.FileDescriptorProto,
) -> bytes:
    comparable = descriptor_pb2.FileDescriptorProto()
    comparable.CopyFrom(file_proto)
    comparable.ClearField("source_code_info")
    return comparable.SerializeToString(deterministic=True)


def _component_suffix(full_name: str, suffix: str) -> bool:
    return full_name == suffix or full_name.endswith(f".{suffix}")


def _symbol_full_name(symbol: DescriptorSymbol) -> str:
    full_name = getattr(symbol, "full_name", None)
    if not isinstance(full_name, str):
        raise TypeError(f"descriptor {type(symbol).__name__} has no full name")
    return full_name


def _is_method(symbol: DescriptorSymbol) -> TypeGuard[MethodDescriptor]:
    return isinstance(symbol, MethodDescriptor)


def source_from_registry(
    registry: FileDescriptorRegistry,
) -> IndexedDescriptorSource:
    """Build an indexed descriptor source from a validated registry."""
    database = descriptor_database.DescriptorDatabase()
    services: list[str] = []
    for file_proto in registry.files.values():
        try:
            database.Add(file_proto)
        except (KeyError, TypeError, ValueError) as error:
            raise DescriptorBuildError(file_proto.name, error) from error
        services.extend(
            f"{file_proto.package}.{service.name}".lstrip(".")
            for service in file_proto.service
        )
    pool = descriptor_pool.DescriptorPool(database)
    source = IndexedDescriptorSource(pool, services, registry.files)
    source.registry = registry

    from .source_protos import register_file_proto

    for file_name, file_proto in registry.files.items():
        try:
            descriptor = pool.FindFileByName(file_name)
        except (KeyError, TypeError, ValueError) as error:
            raise DescriptorBuildError(file_name, error) from error
        register_file_proto(descriptor, file_proto)
    return source


def source_from_file_protos(
    file_protos: Sequence[descriptor_pb2.FileDescriptorProto],
) -> IndexedDescriptorSource:
    """Build an indexed source from file descriptor protos."""
    registry = FileDescriptorRegistry()
    for file_proto in file_protos:
        registry.add(file_proto, "descriptor input")
    return source_from_registry(registry)
