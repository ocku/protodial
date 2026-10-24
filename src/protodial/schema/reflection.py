"""Discover protobuf descriptors through gRPC reflection."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import cast

from google.protobuf import (
    descriptor_database,
    descriptor_pb2,
    descriptor_pool,
    message_factory,
)
from google.protobuf.message import DecodeError, Message

from ..errors import CliError, add_secondary_error
from ..request_metadata.types import Metadata
from .contracts import (
    GrpcModule,
    ReflectionCall,
    ReflectionChannel,
    ReflectionResponse,
    ReflectionWireModule,
    RpcError,
)
from .registry import FileDescriptorRegistry, IndexedDescriptorSource

REFLECTION_SERVICES = frozenset(
    {
        "grpc.reflection.v1.ServerReflection",
        "grpc.reflection.v1alpha.ServerReflection",
    }
)

_RESPONSE_FIELD_BY_REQUEST = {
    "file_by_filename": "file_descriptor_response",
    "file_containing_symbol": "file_descriptor_response",
    "file_containing_extension": "file_descriptor_response",
    "all_extension_numbers_of_type": "all_extension_numbers_response",
    "list_services": "list_services_response",
}
_RESPONSE_FIELDS = frozenset(
    (*_RESPONSE_FIELD_BY_REQUEST.values(), "error_response")
)
_SERVICE_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)


def _response_field(reply: ReflectionResponse) -> str | None:
    """Return the active response variant."""

    which_oneof = getattr(reply, "WhichOneof", None)
    if callable(which_oneof):
        return cast(str | None, which_oneof("message_response"))

    has_field = getattr(reply, "HasField", None)
    if not callable(has_field):
        return None
    active = [field for field in _RESPONSE_FIELDS if has_field(field)]
    if len(active) > 1:
        raise CliError("reflection protocol error: multiple response variants")
    return active[0] if active else None


def _validate_response_identity(
    reply: ReflectionResponse, request: Message
) -> None:
    """Require a reflection response to identify the request it answers."""

    has_field = getattr(reply, "HasField", None)
    if not callable(has_field) or not has_field("original_request"):
        raise CliError(
            "reflection protocol error: response has no original_request"
        )
    if (
        reply.original_request.SerializeToString()
        != request.SerializeToString()
    ):
        raise CliError(
            "reflection protocol error: "
            "response original_request does not match"
        )

    valid_host = getattr(reply, "valid_host", "")
    request_host = getattr(request, "host", "")
    if valid_host and valid_host != request_host:
        raise CliError(
            "reflection protocol error: "
            f"response valid_host {valid_host!r} does not match "
            f"request host {request_host!r}"
        )


def _stable_wire_module(wire_v1alpha: ModuleType) -> ReflectionWireModule:
    try:
        module = import_module("grpc_reflection.v1.reflection_pb2")
        return cast(ReflectionWireModule, cast(object, module))
    except ImportError:
        # Some grpcio-reflection releases package only v1alpha. Build v1 classes
        # from that wire-identical schema rather than using v1alpha classes on
        # the stable endpoint.
        v1_file = descriptor_pb2.FileDescriptorProto.FromString(
            wire_v1alpha.DESCRIPTOR.serialized_pb
        )
        v1_file.name = "grpc/reflection/v1/reflection.proto"
        v1_file.package = "grpc.reflection.v1"

        def update_message_types(
            message: descriptor_pb2.DescriptorProto,
        ) -> None:
            for field in message.field:
                if field.type_name.startswith(".grpc.reflection.v1alpha."):
                    field.type_name = field.type_name.replace(
                        ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                    )
            for nested in message.nested_type:
                update_message_types(nested)

        for message in v1_file.message_type:
            update_message_types(message)
        for service in v1_file.service:
            for method in service.method:
                method.input_type = method.input_type.replace(
                    ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                )
                method.output_type = method.output_type.replace(
                    ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                )

        v1_pool = descriptor_pool.DescriptorPool()
        v1_pool.Add(v1_file)
        module = SimpleNamespace(
            ServerReflectionRequest=message_factory.GetMessageClass(
                v1_pool.FindMessageTypeByName(
                    "grpc.reflection.v1.ServerReflectionRequest"
                )
            ),
            ServerReflectionResponse=message_factory.GetMessageClass(
                v1_pool.FindMessageTypeByName(
                    "grpc.reflection.v1.ServerReflectionResponse"
                )
            ),
        )
        return cast(ReflectionWireModule, cast(object, module))


class ReflectionDatabase(descriptor_database.DescriptorDatabase):
    """Fetch and cache descriptors lazily through server reflection."""

    def __init__(
        self,
        channel: ReflectionChannel,
        metadata: Metadata,
        timeout: float,
        grpc: GrpcModule,
        registry: FileDescriptorRegistry | None,
        wire_modules: dict[str, ReflectionWireModule],
        host: str = "",
    ) -> None:
        super().__init__()
        self.version: str | None = None
        self._services: tuple[str, ...] | None = None
        self._missing_files: set[str] = set()
        self._missing_symbols: set[str] = set()
        self._missing_extensions: set[tuple[str, int]] = set()
        self._extension_numbers: dict[str, tuple[int, ...]] = {}
        self.registry = (
            registry if registry is not None else FileDescriptorRegistry()
        )
        self.file_protos = self.registry.files
        self._channel = channel
        self._metadata = metadata
        self._timeout = timeout
        self._grpc = grpc
        self._wire_modules = wire_modules
        self._host = host

    def _record_version(self, version: str) -> None:
        """Record and immediately report the negotiated wire version."""
        if self.version == version:
            return
        self.version = version
        from ..presentation.diagnostics.context import current_sink
        from ..presentation.diagnostics.models import DiagnosticEvent

        sink = current_sink()
        if sink is not None:
            sink.emit(
                DiagnosticEvent(
                    "reflection.negotiated",
                    level=2,
                    fields={"version": version},
                )
            )

    @staticmethod
    def _request_shape(
        request_fields: Mapping[str, object],
    ) -> tuple[str, str]:
        """Validate a query and return its request/response field names."""
        if len(request_fields) != 1:
            raise TypeError(
                "reflection query requires exactly one request field"
            )
        request_field = next(iter(request_fields))
        try:
            expected_response = _RESPONSE_FIELD_BY_REQUEST[request_field]
        except KeyError:
            raise TypeError(
                f"unsupported reflection request field: {request_field}"
            ) from None
        return request_field, expected_response

    def _start_query(
        self,
        version: str,
        request_fields: Mapping[str, object],
    ) -> tuple[Message, ReflectionCall]:
        """Construct one version-specific request and start its stream."""
        wire = self._wire_modules[version]
        rpc = self._channel.stream_stream(
            f"/grpc.reflection.{version}.ServerReflection/ServerReflectionInfo",
            request_serializer=wire.ServerReflectionRequest.SerializeToString,
            response_deserializer=wire.ServerReflectionResponse.FromString,
        )
        request = wire.ServerReflectionRequest(
            host=self._host, **request_fields
        )
        call = rpc(
            iter((request,)),
            metadata=self._metadata,
            timeout=self._timeout,
        )
        return request, call

    def _handle_error_response(
        self, reply: ReflectionResponse, version: str
    ) -> bool:
        """Raise a reflected error or request fallback for UNIMPLEMENTED."""
        error = reply.error_response
        if (
            error.error_code == self._grpc.StatusCode.UNIMPLEMENTED.value[0]
            and self.version is None
        ):
            return True
        self._record_version(version)
        if error.error_code == self._grpc.StatusCode.NOT_FOUND.value[0]:
            raise KeyError(error.error_message)
        raise CliError(f"reflection: {error.error_code}: {error.error_message}")

    @staticmethod
    def _validate_response_field(
        response_field: str | None,
        request_field: str,
        expected_response: str,
    ) -> None:
        """Require the response variant associated with the request."""
        if response_field == expected_response:
            return
        received = response_field or "no response variant"
        raise CliError(
            "reflection protocol error: "
            f"{request_field!r} request received {received!r}; "
            f"expected {expected_response!r}"
        )

    def _store_file_descriptors(
        self, reply: ReflectionResponse, version: str
    ) -> None:
        """Validate a complete response before committing any of its files."""
        try:
            trial_registry = FileDescriptorRegistry()
            trial_registry.files.update(self.registry.files)
            trial_registry.origins.update(self.registry.origins)

            for data in reply.file_descriptor_response.file_descriptor_proto:
                proto = descriptor_pb2.FileDescriptorProto.FromString(data)
                if not proto.name:
                    raise ValueError("file descriptor has no name")
                trial_registry.add(proto, f"reflection {version}")

            trial_database = descriptor_database.DescriptorDatabase()
            for proto in trial_registry.files.values():
                trial_database.Add(proto)

            trial_pool = descriptor_pool.DescriptorPool(trial_database)
            for file_name in trial_registry.files:
                trial_pool.FindFileByName(file_name)
        except (CliError, DecodeError, KeyError, TypeError, ValueError):
            raise CliError(
                "reflection protocol error: invalid file descriptor payload"
            ) from None

        # Validation above exercised the same database implementation and the
        # complete dependency graph, so installing the staged maps cannot fail
        # partway through an Add operation. Keep the public registry mappings
        # alive because descriptor sources retain references to them.
        self.__dict__.update(trial_database.__dict__)
        self.registry.files.update(trial_registry.files)
        self.registry.origins.update(trial_registry.origins)

    @staticmethod
    def _cancel_call(
        call: ReflectionCall | None, primary: BaseException | None
    ) -> None:
        """Cancel a reflection stream without replacing its primary error."""
        if call is None:
            return
        try:
            call.cancel()
        except BaseException as cleanup_error:
            if primary is None:
                raise
            add_secondary_error(
                primary,
                "reflection cancellation also failed",
                cleanup_error,
            )

    def _query_version(
        self,
        version: str,
        request_fields: Mapping[str, object],
        request_field: str,
        expected_response: str,
    ) -> ReflectionResponse | None:
        """Run one reflection version, returning None when fallback is valid."""
        call: ReflectionCall | None = None
        primary: BaseException | None = None
        try:
            request, call = self._start_query(version, request_fields)
            reply = cast(ReflectionResponse, cast(object, next(call)))
            _validate_response_identity(reply, request)
            response_field = _response_field(reply)
            if response_field == "error_response":
                if self._handle_error_response(reply, version):
                    return None
            self._validate_response_field(
                response_field, request_field, expected_response
            )
            self._record_version(version)
            self._store_file_descriptors(reply, version)
            return reply
        except StopIteration:
            primary = CliError("reflection server returned no response")
            raise primary from None
        except Exception as error:
            primary = error
            if not isinstance(error, self._grpc.RpcError):
                raise
            rpc_error = cast(RpcError, error)
            if (
                rpc_error.code() == self._grpc.StatusCode.UNIMPLEMENTED
                and self.version is None
            ):
                primary = None
                return None
            raise
        except BaseException as error:
            primary = error
            raise
        finally:
            self._cancel_call(call, primary)

    def query(self, **request_fields: object) -> ReflectionResponse:
        """Run one validated reflection query with version fallback."""
        request_field, expected_response = self._request_shape(request_fields)
        versions = (self.version,) if self.version else ("v1", "v1alpha")
        for version in versions:
            reply = self._query_version(
                version,
                request_fields,
                request_field,
                expected_response,
            )
            if reply is not None:
                return reply
        raise CliError(
            "server reflection is unavailable; provide --proto or --protoset"
        )

    def get_services(self) -> Sequence[str]:
        """Return and cache reflected service names."""
        if self._services is None:
            response = self.query(list_services="")
            names = tuple(
                service.name
                for service in response.list_services_response.service
            )
            if any(_SERVICE_NAME.fullmatch(name) is None for name in names):
                raise CliError(
                    "reflection protocol error: invalid service name"
                )
            self._services = tuple(sorted(dict.fromkeys(names)))
        return self._services

    def FindFileByName(self, name: str) -> descriptor_pb2.FileDescriptorProto:
        """Find or reflect a file by its protobuf import name."""
        try:
            return super().FindFileByName(name)
        except KeyError:
            if name in self._missing_files:
                raise
            try:
                self.query(file_by_filename=name)
            except KeyError:
                self._missing_files.add(name)
                raise
            return super().FindFileByName(name)

    def FindFileContainingSymbol(
        self, symbol: str
    ) -> descriptor_pb2.FileDescriptorProto:
        """Find or reflect the file declaring a symbol."""
        try:
            return super().FindFileContainingSymbol(symbol)
        except KeyError:
            if symbol in self._missing_symbols:
                raise
            try:
                self.query(file_containing_symbol=symbol)
            except KeyError:
                self._missing_symbols.add(symbol)
                raise
            return super().FindFileContainingSymbol(symbol)

    def FindFileContainingExtension(
        self, extendee_name: str, extension_number: int
    ) -> descriptor_pb2.FileDescriptorProto:
        """Find or reflect the file declaring an extension."""
        normalized = extendee_name.lstrip(".")
        found = _find_extension_file(
            self.file_protos.values(), normalized, extension_number
        )
        if found is not None:
            return found

        key = (normalized, extension_number)
        if key in self._missing_extensions:
            raise KeyError(key)
        try:
            self.query(
                file_containing_extension={
                    "containing_type": normalized,
                    "extension_number": extension_number,
                }
            )
        except KeyError:
            self._missing_extensions.add(key)
            raise

        found = _find_extension_file(
            self.file_protos.values(), normalized, extension_number
        )
        if found is None:
            self._missing_extensions.add(key)
            raise KeyError(key)
        return found

    def FindAllExtensionNumbers(self, extendee_name: str) -> list[int]:
        """Return and cache extension numbers for a message type."""
        normalized = extendee_name.lstrip(".")
        cached = self._extension_numbers.get(normalized)
        if cached is not None:
            return list(cached)

        response = self.query(all_extension_numbers_of_type=normalized)
        numbers = set(response.all_extension_numbers_response.extension_number)
        numbers.update(
            _known_extension_numbers(self.file_protos.values(), normalized)
        )
        result = sorted(numbers)
        self._extension_numbers[normalized] = tuple(result)
        return result


def _extensions_in_message(
    message: descriptor_pb2.DescriptorProto,
) -> Sequence[descriptor_pb2.FieldDescriptorProto]:
    extensions = list(message.extension)
    for nested in message.nested_type:
        extensions.extend(_extensions_in_message(nested))
    return extensions


def _file_extensions(
    file_proto: descriptor_pb2.FileDescriptorProto,
) -> Sequence[descriptor_pb2.FieldDescriptorProto]:
    extensions = list(file_proto.extension)
    for message in file_proto.message_type:
        extensions.extend(_extensions_in_message(message))
    return extensions


def _find_extension_file(
    file_protos: Iterable[descriptor_pb2.FileDescriptorProto],
    extendee_name: str,
    extension_number: int,
) -> descriptor_pb2.FileDescriptorProto | None:
    for file_proto in file_protos:
        if any(
            extension.extendee.lstrip(".") == extendee_name
            and extension.number == extension_number
            for extension in _file_extensions(file_proto)
        ):
            return file_proto
    return None


def _known_extension_numbers(
    file_protos: Iterable[descriptor_pb2.FileDescriptorProto],
    extendee_name: str,
) -> set[int]:
    return {
        extension.number
        for file_proto in file_protos
        for extension in _file_extensions(file_proto)
        if extension.extendee.lstrip(".") == extendee_name
    }


class ReflectionDescriptorSource(IndexedDescriptorSource):
    """A descriptor source backed by lazy server reflection."""

    def __init__(
        self,
        channel: ReflectionChannel,
        metadata: Metadata,
        timeout: float,
        grpc: GrpcModule,
        registry: FileDescriptorRegistry | None = None,
        host: str = "",
    ) -> None:
        database = reflection_database(
            channel, metadata, timeout, grpc, registry, host
        )
        super().__init__(
            descriptor_pool.DescriptorPool(database),
            database.get_services(),
            database.file_protos,
        )
        self.database = database


def reflection_database(
    channel: ReflectionChannel,
    metadata: Metadata,
    timeout: float,
    grpc: GrpcModule,
    registry: FileDescriptorRegistry | None = None,
    host: str = "",
) -> ReflectionDatabase:
    """Build a reflection database supporting v1 and v1alpha."""
    from grpc_reflection.v1alpha import reflection_pb2 as wire_v1alpha

    wire_v1 = _stable_wire_module(wire_v1alpha)
    wire_v1alpha_typed = cast(ReflectionWireModule, cast(object, wire_v1alpha))
    return ReflectionDatabase(
        channel,
        metadata,
        timeout,
        grpc,
        registry,
        {"v1": wire_v1, "v1alpha": wire_v1alpha_typed},
        host,
    )
