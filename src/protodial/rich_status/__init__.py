"""Rich gRPC status models, decoding, and rendering."""

from .decoding import (
    DEFAULT_MAX_DETAIL_BYTES,
    DEFAULT_MAX_DETAILS,
    DEFAULT_MAX_RECURSION_DEPTH,
    DEFAULT_MAX_STATUS_BYTES,
    STATUS_DETAILS_KEY,
    decode_rich_status,
    decode_status_details,
    extract_status_details_bin,
)
from .models import DecodedStatus, DecodedStatusDetail, RichStatus, StatusDetail
from .rendering import (
    DEFAULT_MAX_RENDERED_CHARS,
    render_rich_status,
    write_rich_status,
)

__all__ = [
    "DEFAULT_MAX_DETAIL_BYTES",
    "DEFAULT_MAX_DETAILS",
    "DEFAULT_MAX_RECURSION_DEPTH",
    "DEFAULT_MAX_RENDERED_CHARS",
    "DEFAULT_MAX_STATUS_BYTES",
    "STATUS_DETAILS_KEY",
    "DecodedStatus",
    "DecodedStatusDetail",
    "RichStatus",
    "StatusDetail",
    "decode_rich_status",
    "decode_status_details",
    "extract_status_details_bin",
    "render_rich_status",
    "write_rich_status",
]
