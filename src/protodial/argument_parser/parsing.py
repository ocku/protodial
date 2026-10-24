"""Top-level command parsing and configuration assembly."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from ..commands.models import (
    Command,
    ConnectionOptions,
    DiagnosticOptions,
    InvokeOptions,
    MetadataOptions,
    ParsedTarget,
    SchemaOptions,
    TlsOptions,
)
from ..commands.operations import Operation
from .construction import build_invocation_parser, build_parser
from .resolution import resolve_command, resolve_transport_mode
from .validation import (
    validate_common_options,
    validate_invocation_options,
    validate_operation_options,
)


def _build_command(
    namespace: argparse.Namespace,
    target: ParsedTarget,
    operation: Operation,
    invoke_options: InvokeOptions | None,
    transport_mode: str,
) -> Command:
    verbosity = namespace.verbose
    return Command(
        target=target,
        operation=operation,
        schema=SchemaOptions(
            proto=tuple(namespace.proto),
            proto_path=tuple(namespace.proto_path),
            protoset=tuple(namespace.protoset),
            protoset_out=namespace.protoset_out,
            use_reflection=namespace.use_reflection,
        ),
        transport=TlsOptions(
            mode=transport_mode,
            ca_paths=tuple(namespace.cacert),
            cert=namespace.cert,
            key=namespace.key,
            key_password=namespace.key_password,
            server_name=namespace.servername,
            authority=namespace.authority,
        ),
        connection=ConnectionOptions(
            connect_timeout=namespace.connect_timeout,
            max_recv_message_size=namespace.max_recv_message_size,
            max_send_message_size=namespace.max_send_message_size,
            compression=namespace.compression,
            keepalive_time=namespace.keepalive_time,
            user_agent=namespace.user_agent,
        ),
        metadata=MetadataOptions(
            header=tuple(namespace.header),
            reflect_header=tuple(namespace.reflect_header),
            rpc_header=tuple(getattr(namespace, "rpc_header", ())),
            expand_headers=namespace.expand_headers,
        ),
        diagnostics=DiagnosticOptions(
            show_headers=verbosity > 0 or namespace.show_headers,
            show_trailers=verbosity > 0 or namespace.show_trailers,
            show_status=verbosity > 0 or namespace.show_status,
            verbosity=verbosity,
            format_error=namespace.format_error or verbosity > 0,
            diagnostic_format=namespace.diagnostic_format,
            color=namespace.color,
        ),
        timeout=namespace.timeout,
        invoke_options=invoke_options,
        get_template=namespace.get_template,
        template_depth=getattr(namespace, "template_depth", 8),
        show_reflection=namespace.show_reflection,
    )


def parse_arguments(argv: Sequence[str] | None = None) -> Command:
    """Parse command-line arguments into validated command configuration."""
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"list", "describe"}:
        # Invocation is the default mode. Discovery commands must still occupy
        # argv[0], so `--option list ...` is never reinterpreted as discovery.
        parser = build_invocation_parser()
    namespace = parser.parse_args(arguments)
    if not hasattr(namespace, "diagnostic_format"):
        diagnostic_format = os.environ.get(
            "PROTODIAL_DIAGNOSTIC_FORMAT", "human"
        )
        if diagnostic_format not in {"human", "json"}:
            parser.error(
                "PROTODIAL_DIAGNOSTIC_FORMAT must be one of: human, json"
            )
        namespace.diagnostic_format = diagnostic_format
    if not hasattr(namespace, "color"):
        namespace.color = "auto"
    target, operation = resolve_command(parser, namespace)
    invoke_options = validate_invocation_options(parser, namespace, operation)
    validate_common_options(parser, namespace)
    transport_mode = resolve_transport_mode(parser, namespace, target)
    validate_operation_options(parser, namespace, operation)
    return _build_command(
        namespace, target, operation, invoke_options, transport_mode
    )
