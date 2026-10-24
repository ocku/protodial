"""Request parsing and response rendering for supported wire formats."""

from .dispatch import iter_requests
from .json_input import JSON_READ_SIZE, iter_json_requests, iter_json_values
from .models import (
    InputBufferOptions,
    JsonInputOptions,
    JsonOutputOptions,
    JsonScalar,
    JsonValue,
    MessageError,
    MessageParseError,
    MessageTooLarge,
    MethodStreaming,
    OutputOptions,
    RawInputOptions,
    RequestOptions,
    SendLimitOptions,
    TextInputOptions,
    TruncatedFrameBody,
    TruncatedFrameHeader,
)
from .output import message_to_json, render_response, write_response
from .protobuf_input import iter_protobuf_requests
from .raw_input import iter_raw_requests
from .text_input import (
    TEXT_STREAM_DELIMITER,
    iter_text_documents,
    iter_text_requests,
)

__all__ = [
    "InputBufferOptions",
    "JSON_READ_SIZE",
    "JsonInputOptions",
    "JsonOutputOptions",
    "JsonScalar",
    "JsonValue",
    "MessageError",
    "MessageParseError",
    "MessageTooLarge",
    "MethodStreaming",
    "OutputOptions",
    "RawInputOptions",
    "RequestOptions",
    "SendLimitOptions",
    "TEXT_STREAM_DELIMITER",
    "TextInputOptions",
    "TruncatedFrameBody",
    "TruncatedFrameHeader",
    "iter_json_requests",
    "iter_json_values",
    "iter_protobuf_requests",
    "iter_raw_requests",
    "iter_requests",
    "iter_text_documents",
    "iter_text_requests",
    "message_to_json",
    "render_response",
    "write_response",
]
