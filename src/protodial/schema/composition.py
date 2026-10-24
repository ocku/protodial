"""Compose multiple protobuf descriptor sources."""

from __future__ import annotations

from collections.abc import Sequence

from google.protobuf import descriptor_database, descriptor_pb2, descriptor_pool

from .reflection import ReflectionDescriptorSource
from .registry import IndexedDescriptorSource


class CompositeDatabase:
    """Resolve local descriptors before consulting reflection."""

    def __init__(
        self,
        local: descriptor_database.DescriptorDatabase,
        reflection: descriptor_database.DescriptorDatabase,
    ) -> None:
        self._local = local
        self._reflection = reflection

    def FindFileByName(self, name: str) -> descriptor_pb2.FileDescriptorProto:
        """Find a file by its protobuf import name."""
        try:
            return self._local.FindFileByName(name)
        except KeyError:
            return self._reflection.FindFileByName(name)

    def FindFileContainingSymbol(
        self, symbol: str
    ) -> descriptor_pb2.FileDescriptorProto:
        """Find the file declaring a fully qualified symbol."""
        try:
            return self._local.FindFileContainingSymbol(symbol)
        except KeyError:
            return self._reflection.FindFileContainingSymbol(symbol)

    def FindFileContainingExtension(
        self, extendee_name: str, extension_number: int
    ) -> descriptor_pb2.FileDescriptorProto:
        """Find the file declaring a numbered extension."""
        try:
            local_file = self._local.FindFileContainingExtension(
                extendee_name, extension_number
            )
        except KeyError:
            local_file = None
        if local_file is not None:
            return local_file
        reflected_file = self._reflection.FindFileContainingExtension(
            extendee_name, extension_number
        )
        if reflected_file is None:
            raise KeyError((extendee_name, extension_number))
        return reflected_file

    def FindAllExtensionNumbers(self, extendee_name: str) -> Sequence[int]:
        """Return known extension numbers from both sources."""
        local_numbers = self._local.FindAllExtensionNumbers(extendee_name)
        remote_numbers = self._reflection.FindAllExtensionNumbers(extendee_name)
        return sorted(set(local_numbers) | set(remote_numbers))


class CompositeDescriptorSource(IndexedDescriptorSource):
    """A single descriptor pool backed by local files and lazy reflection."""

    def __init__(
        self,
        local: IndexedDescriptorSource,
        reflection: ReflectionDescriptorSource,
    ) -> None:
        registry = reflection.database.registry
        for name, file_proto in local.file_protos.items():
            origin = local.registry
            label = (
                origin.origins[name]
                if origin is not None
                else "local descriptors"
            )
            registry.add(file_proto, label)

        local_database = descriptor_database.DescriptorDatabase()
        for file_proto in local.file_protos.values():
            local_database.Add(file_proto)

        database = CompositeDatabase(local_database, reflection.database)
        services = (*local.list_services(), *reflection.list_services())
        super().__init__(
            descriptor_pool.DescriptorPool(database), services, registry.files
        )
        self.registry = registry
        self.local = local
        self.reflection = reflection
