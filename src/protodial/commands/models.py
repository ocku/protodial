"""Immutable command configuration produced by CLI argument parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..formats import MessageFormat
from .operations import Operation

DEFAULT_CHUNK_SIZE = 64 * 1024
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_INPUT_BUFFER = 4 * 1024 * 1024

TargetTransport = Literal["plaintext", "tls"]


@dataclass(frozen=True)
class ParsedTarget:
    """A validated grpcio channel target and its CLI-derived attributes."""

    channel: str
    method: str | None = None
    transport: TargetTransport | None = None
    authority: str | None = None
    tunnel_host: str | None = None
    tunnel_port: int | None = None


@dataclass(frozen=True)
class SchemaOptions:
    """Schema sources and descriptor-export settings."""

    proto: tuple[str, ...]
    proto_path: tuple[str, ...]
    protoset: tuple[str, ...]
    protoset_out: str | None = None
    use_reflection: bool = False


@dataclass(frozen=True)
class TlsOptions:
    """TLS mode, trust roots, and client identity settings."""

    mode: str
    ca_paths: tuple[str, ...]
    cert: str | None
    key: str | None
    server_name: str | None = None
    authority: str | None = None
    key_password: str | None = None

    @property
    def plaintext(self) -> bool:
        """Return whether transport security is disabled."""
        return self.mode == "plaintext"


@dataclass(frozen=True)
class ConnectionOptions:
    """Channel readiness, limits, compression, and keepalive settings."""

    connect_timeout: float
    max_recv_message_size: int | None
    max_send_message_size: int | None
    compression: str
    keepalive_time: float | None
    user_agent: str | None


@dataclass(frozen=True)
class MetadataOptions:
    """Shared and operation-specific request metadata."""

    header: tuple[str, ...]
    reflect_header: tuple[str, ...] = ()
    rpc_header: tuple[str, ...] = ()
    expand_headers: bool = False


@dataclass(frozen=True)
class DiagnosticOptions:
    """Diagnostic output and rich-error rendering settings."""

    show_headers: bool
    show_trailers: bool
    show_status: bool
    verbosity: int = 0
    format_error: bool = False
    diagnostic_format: str = "human"
    color: str = "auto"


@dataclass(frozen=True)
class InvokeOptions:
    """Request input and response output settings for an invocation."""

    data: str | None
    input_format: MessageFormat
    output_format: MessageFormat
    input_field: str | None
    output_field: str | None
    chunk_size: int
    allow_unknown_fields: bool = False
    emit_defaults: bool = False
    max_input_buffer: int | None = DEFAULT_INPUT_BUFFER
    max_send_message_size: int | None = None
    wait_for_ready: bool = False
    fields: tuple[tuple[str, str], ...] = ()
    data_argument: str = "--data"
    data_option_strings: tuple[str, ...] = ("-d", "--data")
    data_option_value: str | None = None


@dataclass(frozen=True)
class Command:
    """Validated immutable command-line configuration."""

    target: ParsedTarget
    operation: Operation
    schema: SchemaOptions
    transport: TlsOptions
    connection: ConnectionOptions
    metadata: MetadataOptions
    diagnostics: DiagnosticOptions
    timeout: float
    invoke_options: InvokeOptions | None
    get_template: bool
    template_depth: int
    show_reflection: bool
