"""Dispatch request parsing according to the selected input format."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, TypeVar

from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message

from ..formats import MessageFormat
from .json_input import iter_json_requests
from .models import MessageError, MethodStreaming, RequestOptions
from .protobuf_input import iter_protobuf_requests
from .raw_input import iter_raw_requests
from .text_input import iter_text_requests

_MessageT = TypeVar("_MessageT", bound=Message)


def iter_requests(
    source: BinaryIO,
    options: RequestOptions,
    method: MethodStreaming,
    message_class: type[_MessageT],
    pool: DescriptorPool,
    raw_field: str | None,
) -> Iterator[_MessageT]:
    """Parse requests for the invocation and RPC streaming configuration."""

    input_format = options.input_format
    streaming = bool(method.client_streaming)
    if options.data is None:
        if options.fields:
            raise MessageError("--field requires input data")
        if not streaming:
            yield message_class()
        return
    if input_format == MessageFormat.JSON:
        yield from iter_json_requests(
            source, streaming, message_class, pool, options
        )
    elif input_format == MessageFormat.TEXT:
        yield from iter_text_requests(
            source, streaming, message_class, pool, options
        )
    elif input_format == MessageFormat.PROTOBUF:
        yield from iter_protobuf_requests(
            source, streaming, message_class, options
        )
    elif input_format == MessageFormat.RAW:
        if raw_field is None:
            raise MessageError("raw input requires a selected bytes field")
        yield from iter_raw_requests(
            source,
            streaming,
            options.chunk_size,
            message_class,
            raw_field,
            options,
            options.fields,
            pool,
        )
    else:
        raise MessageError(f"unsupported input format: {input_format}")
