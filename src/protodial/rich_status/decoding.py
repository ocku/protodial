"""Decode google.rpc.Status messages and detail payloads."""

from __future__ import annotations

import base64
import binascii
import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import DecodeError, Message

from ..request_metadata.types import MetadataIterable
from .models import DecodedStatusDetail, RichStatus

STATUS_DETAILS_KEY = "grpc-status-details-bin"
DEFAULT_MAX_STATUS_BYTES = 1024 * 1024
DEFAULT_MAX_DETAIL_BYTES = 256 * 1024
DEFAULT_MAX_DETAILS = 64
DEFAULT_MAX_RECURSION_DEPTH = 32


class _PackedAny(Protocol):
    type_url: str
    value: bytes


class _StatusMessage(Protocol):
    code: int
    message: str
    details: Sequence[_PackedAny]

    def ParseFromString(self, data: bytes) -> int: ...


@runtime_checkable
class _StatusValue(Protocol):
    value: object


@runtime_checkable
class _NamedStatus(Protocol):
    name: str


CODE_NAMES = {
    0: "OK",
    1: "CANCELLED",
    2: "UNKNOWN",
    3: "INVALID_ARGUMENT",
    4: "DEADLINE_EXCEEDED",
    5: "NOT_FOUND",
    6: "ALREADY_EXISTS",
    7: "PERMISSION_DENIED",
    8: "RESOURCE_EXHAUSTED",
    9: "FAILED_PRECONDITION",
    10: "ABORTED",
    11: "OUT_OF_RANGE",
    12: "UNIMPLEMENTED",
    13: "INTERNAL",
    14: "UNAVAILABLE",
    15: "DATA_LOSS",
    16: "UNAUTHENTICATED",
}


def _decode_limit(value: object, name: str) -> int:
    """Return a validated nonnegative rich-status decode limit."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def extract_status_details_bin(
    trailing_metadata: MetadataIterable,
) -> bytes | None:
    """Return the last rich-status trailer, matching grpcio duplicates."""

    found: bytes | None = None
    for key, value in trailing_metadata:
        if str(key).lower() != STATUS_DETAILS_KEY:
            continue
        if isinstance(value, bytes):
            found = value
        else:
            try:
                found = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(
                    f"{STATUS_DETAILS_KEY} is not valid base64"
                ) from None
    return found


@dataclass(frozen=True, slots=True)
class _DecodeLimits:
    status_bytes: int
    detail_bytes: int
    details: int
    recursion_depth: int


def _validated_limits(
    max_status_bytes: int,
    max_detail_bytes: int,
    max_details: int,
    max_recursion_depth: int,
) -> _DecodeLimits:
    return _DecodeLimits(
        _decode_limit(max_status_bytes, "max_status_bytes"),
        _decode_limit(max_detail_bytes, "max_detail_bytes"),
        _decode_limit(max_details, "max_details"),
        _decode_limit(max_recursion_depth, "max_recursion_depth"),
    )


def _status_data(
    value: bytes | MetadataIterable | None,
) -> tuple[bytes | None, str | None]:
    if value is None or isinstance(value, bytes):
        return value, None
    try:
        return extract_status_details_bin(value), None
    except (TypeError, ValueError) as error:
        return None, str(error)


def _parse_status(data: bytes) -> tuple[_StatusMessage | None, str | None]:
    message = cast(_StatusMessage, status_message_class()())
    try:
        message.ParseFromString(data)
    except DecodeError as error:
        return None, f"could not decode {STATUS_DETAILS_KEY}: {error}"
    return message, None


def _decode_details(
    status: _StatusMessage,
    descriptor_pool: DescriptorPool | None,
    limits: _DecodeLimits,
) -> tuple[tuple[DecodedStatusDetail, ...], list[str]]:
    packed_details = status.details
    details = tuple(
        decode_any(
            packed.type_url,
            bytes(packed.value),
            descriptor_pool,
            limits.detail_bytes,
            limits.recursion_depth,
        )
        for packed in packed_details[: limits.details]
    )
    warnings: list[str] = []
    omitted = len(packed_details) - limits.details
    if omitted > 0:
        warnings.append(
            f"omitted {omitted} status detail(s); limit is {limits.details}"
        )
    return details, warnings


def _consistency_warnings(
    status: _StatusMessage,
    transport_code: object | None,
    transport_message: str | None,
) -> list[str]:
    warnings: list[str] = []
    inner_code = int(status.code)
    outer_code = code_number(transport_code)
    if outer_code is not None and outer_code != inner_code:
        warnings.append(
            "rich status code "
            f"{format_code(inner_code)} does not match transport status "
            f"{format_code(outer_code)}"
        )
    if transport_message is not None and status.message != transport_message:
        warnings.append(
            "transport status details "
            f"`{transport_message}` differ from rich status message "
            f"`{status.message}`"
        )
    return warnings


def decode_status_details(
    value: bytes | MetadataIterable | None,
    descriptor_pool: DescriptorPool | None = None,
    *,
    transport_code: object | None = None,
    transport_message: str | None = None,
    max_status_bytes: int = DEFAULT_MAX_STATUS_BYTES,
    max_detail_bytes: int = DEFAULT_MAX_DETAIL_BYTES,
    max_details: int = DEFAULT_MAX_DETAILS,
    max_recursion_depth: int = DEFAULT_MAX_RECURSION_DEPTH,
) -> RichStatus | None:
    """Decode ``google.rpc.Status`` and details without hiding failures.

    ``value`` may be raw trailer bytes or the complete trailing metadata
    sequence. Decode problems are represented as warnings instead of exceptions
    so the transport's primary gRPC error can always remain visible.
    """

    limits = _validated_limits(
        max_status_bytes,
        max_detail_bytes,
        max_details,
        max_recursion_depth,
    )

    data, extraction_warning = _status_data(value)
    if extraction_warning is not None:
        return RichStatus(None, "", (), (extraction_warning,))
    if data is None:
        return None

    if len(data) > limits.status_bytes:
        return RichStatus(
            None,
            "",
            (),
            (
                f"rich status is {len(data)} bytes; limit is "
                f"{limits.status_bytes} bytes",
            ),
            len(data),
        )

    status, parse_warning = _parse_status(data)
    if status is None:
        assert parse_warning is not None
        return RichStatus(
            None,
            "",
            (),
            (parse_warning,),
            len(data),
        )

    details, warnings = _decode_details(status, descriptor_pool, limits)
    warnings.extend(
        _consistency_warnings(status, transport_code, transport_message)
    )

    return RichStatus(
        int(status.code),
        status.message,
        details,
        tuple(warnings),
        len(data),
    )


def decode_rich_status(
    trailing_metadata: MetadataIterable,
    descriptor_pool: DescriptorPool | None = None,
    *,
    transport_code: object | None = None,
    transport_message: str | None = None,
    max_status_bytes: int = DEFAULT_MAX_STATUS_BYTES,
    max_detail_bytes: int = DEFAULT_MAX_DETAIL_BYTES,
    max_details: int = DEFAULT_MAX_DETAILS,
    max_recursion_depth: int = DEFAULT_MAX_RECURSION_DEPTH,
) -> RichStatus | None:
    """Decode rich status metadata using the active descriptor pool."""
    return decode_status_details(
        trailing_metadata,
        descriptor_pool,
        transport_code=transport_code,
        transport_message=transport_message,
        max_status_bytes=max_status_bytes,
        max_detail_bytes=max_detail_bytes,
        max_details=max_details,
        max_recursion_depth=max_recursion_depth,
    )


def decode_any(
    type_url: str,
    payload: bytes,
    active_pool: DescriptorPool | None,
    max_detail_bytes: int,
    max_recursion_depth: int,
) -> DecodedStatusDetail:
    """Decode one Any payload into a bounded status detail."""
    type_name = type_url.rsplit("/", 1)[-1] if "/" in type_url else type_url
    if not type_name:
        return DecodedStatusDetail(
            type_url, "", payload, warning="empty Any type URL"
        )
    if len(payload) > max_detail_bytes:
        return DecodedStatusDetail(
            type_url,
            type_name,
            payload,
            warning=(
                f"detail is {len(payload)} bytes; limit is "
                f"{max_detail_bytes} bytes"
            ),
        )

    descriptor = find_message(type_name, active_pool)
    if descriptor is None:
        return DecodedStatusDetail(
            type_url, type_name, payload, warning="unknown message type"
        )
    message = message_class(descriptor)()
    try:
        message.ParseFromString(payload)
    except DecodeError as error:
        return DecodedStatusDetail(
            type_url,
            type_name,
            payload,
            warning=f"could not decode detail: {error}",
        )
    if exceeds_message_depth(message, max_recursion_depth):
        return DecodedStatusDetail(
            type_url,
            type_name,
            payload,
            warning=f"detail nesting exceeds limit of {max_recursion_depth}",
        )
    return DecodedStatusDetail(type_url, type_name, payload, message)


def exceeds_message_depth(message: Message, limit: int, depth: int = 0) -> bool:
    """Return whether a message exceeds the allowed nesting depth."""
    if depth > limit:
        return True
    for field, value in message.ListFields():
        if field.message_type is None:
            continue
        if field.message_type.GetOptions().map_entry:
            map_value = field.message_type.fields_by_name["value"]
            if map_value.message_type is None:
                continue
            children = value.values()
        else:
            children = value if field.is_repeated else (value,)
        if any(
            exceeds_message_depth(child, limit, depth + 1) for child in children
        ):
            return True
    return False


def find_message(
    type_name: str, active_pool: DescriptorPool | None
) -> Descriptor | None:
    """Find a message type in the active or default descriptor pool."""
    from google.protobuf import descriptor_pool

    load_standard_detail_types()
    pools = [active_pool, descriptor_pool.Default()]
    seen: set[int] = set()
    for pool in pools:
        if pool is None or id(pool) in seen:
            continue
        seen.add(id(pool))
        try:
            return pool.FindMessageTypeByName(type_name)
        except (KeyError, TypeError):
            pass
    return None


def load_standard_detail_types() -> None:
    """Register installed standard rich-status detail types."""
    # googleapis-common-protos is deliberately optional. Importing these modules
    # registers all common google.rpc detail descriptors in the default pool.
    for module_name in (
        "google.rpc.status_pb2",
        "google.rpc.error_details_pb2",
    ):
        try:
            importlib.import_module(module_name)
        except ImportError:
            pass


def status_message_class() -> type[Message]:
    """Return a usable google.rpc.Status message class."""
    try:
        status_pb2 = importlib.import_module("google.rpc.status_pb2")
        return status_pb2.Status
    except ImportError:
        pass

    from google.protobuf import any_pb2, descriptor_pb2, descriptor_pool

    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(any_pb2.DESCRIPTOR.serialized_pb)
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="google/rpc/status.proto",
        package="google.rpc",
        syntax="proto3",
        dependency=["google/protobuf/any.proto"],
    )
    message = file_proto.message_type.add(name="Status")
    message.field.add(
        name="code",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    )
    message.field.add(
        name="message",
        number=2,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    message.field.add(
        name="details",
        number=3,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".google.protobuf.Any",
    )
    pool.Add(file_proto)
    return message_class(pool.FindMessageTypeByName("google.rpc.Status"))


def message_class(descriptor: Descriptor) -> type[Message]:
    """Return the runtime message class for a descriptor."""
    from google.protobuf import message_factory

    return message_factory.GetMessageClass(descriptor)


def code_number(code: object | None) -> int | None:
    """Extract an integer from a gRPC status-code value."""
    if code is None:
        return None
    if isinstance(code, int):
        return code
    value = code.value if isinstance(code, _StatusValue) else None
    if isinstance(value, int):
        return value
    if isinstance(value, tuple) and value and isinstance(value[0], int):
        return value[0]
    return None


def status_name(code: object) -> str:
    """Return the symbolic name or string form of a status code."""
    return code.name if isinstance(code, _NamedStatus) else str(code)


def format_code(code: int | None) -> str:
    """Format a numeric status code with its symbolic name."""
    if code is None:
        return "UNKNOWN"
    name = CODE_NAMES.get(code)
    return f"{name} ({code})" if name is not None else str(code)
