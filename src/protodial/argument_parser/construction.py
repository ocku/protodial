"""Construction of the protodial command-line parser."""

from __future__ import annotations

import argparse
from functools import cache
from typing import cast

from .. import __version__
from ..commands.models import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_INPUT_BUFFER,
    DEFAULT_TIMEOUT,
)
from ..formats import MessageFormat
from ..presentation.diagnostics import ColorPolicy, DiagnosticFormat
from ..schema.paths import default_proto_paths

_FORMATS = tuple(message_format.value for message_format in MessageFormat)
DIAGNOSTIC_FORMATS = tuple(value.value for value in DiagnosticFormat)
COLOR_POLICIES = tuple(value.value for value in ColorPolicy)
EXAMPLES = """\
Examples:
  protodial [options...] grpc://HOST/SERVICE/METHOD
  protodial [options...] --method SERVICE/METHOD TARGET
  protodial list [options...] TARGET [SERVICE]
  protodial describe [options...] TARGET SYMBOL
  protodial describe [options...] grpc://HOST/SYMBOL
  protodial describe [options...] grpc://HOST/SERVICE/METHOD
  protodial completion bash|zsh
  protodial completion install [--shell bash|zsh]

Run `protodial list --help`, `protodial describe --help`, or
`protodial completion --help` for command-specific options."""


@cache
def option_actions(mode: str) -> tuple[argparse.Action, ...]:
    """Expose registered option metadata for completion and diagnostics."""
    if mode == "invoke":
        parser = build_invocation_parser()
    else:
        root = build_parser()
        subcommands = next(
            action
            for action in root._actions
            if action.nargs == argparse.PARSER
        )
        children = cast(dict[str, argparse.ArgumentParser], subcommands.choices)
        parser = children[mode]
    return tuple(action for action in parser._actions if action.option_strings)


@cache
def value_option_names() -> frozenset[str]:
    """Derive bootstrap option arity from the actual command parsers."""
    return frozenset(
        name
        for mode in ("invoke", "list", "describe")
        for action in option_actions(mode)
        if action.nargs != 0
        for name in action.option_strings
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the complete command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="protodial",
        description=(
            "Call and discover gRPC services using reflection or local schemas."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(
        dest="command_name", metavar="COMMAND", required=True
    )

    list_parser = subparsers.add_parser(
        "list", help="list services or methods", allow_abbrev=False
    )
    _add_shared_arguments(list_parser)
    list_parser.add_argument(
        "--show-reflection",
        action="store_true",
        help="include reflection services in list output",
    )
    list_parser.add_argument("target", help="gRPC target or grpc[s]:// URL")
    list_parser.add_argument(
        "service",
        nargs="?",
        help="service name (may instead be included in the URL path)",
    )
    list_parser.set_defaults(get_template=False)

    describe = subparsers.add_parser(
        "describe", help="describe a schema symbol", allow_abbrev=False
    )
    _add_shared_arguments(describe)
    describe.add_argument(
        "--template",
        action="store_true",
        dest="get_template",
        help=(
            "print sample ProtoJSON for a message or method request instead "
            "of its declaration"
        ),
    )
    describe.add_argument(
        "--template-depth",
        type=int,
        default=argparse.SUPPRESS,
        help="maximum template nesting depth (default: 8)",
    )
    describe.add_argument(
        "target",
        help="gRPC target or grpc[s]:// URL containing the symbol or method",
    )
    describe.add_argument(
        "symbol",
        nargs="?",
        help="schema symbol (when it is not included in TARGET)",
    )
    describe.set_defaults(show_reflection=False)
    return parser


def build_invocation_parser() -> argparse.ArgumentParser:
    """Build the parser for the implicit default invocation command."""
    parser = argparse.ArgumentParser(
        prog="protodial",
        description=(
            "Invoke a gRPC method, or use list/describe for schema discovery."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    _add_shared_arguments(parser)
    _add_invocation_arguments(parser)
    _add_rpc_metadata_argument(parser)
    _add_response_diagnostic_arguments(parser)
    parser.add_argument(
        "--method",
        help="SERVICE/METHOD for a resolver or Unix-socket target",
    )
    parser.add_argument(
        "target",
        help="grpc[s]:// invocation URL or native gRPC channel target",
    )
    parser.set_defaults(
        command_name="invoke", get_template=False, show_reflection=False
    )
    return parser


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    _add_schema_arguments(parser)
    _add_transport_arguments(parser)
    _add_connection_arguments(parser)
    _add_metadata_arguments(parser)
    _add_diagnostic_arguments(parser)


def _add_schema_arguments(parser: argparse.ArgumentParser) -> None:
    proto_paths = default_proto_paths()
    parser.add_argument(
        "--proto", action="append", default=[], help="local .proto (repeatable)"
    )
    parser.add_argument(
        "--protoset",
        action="append",
        default=[],
        help="FileDescriptorSet (repeatable)",
    )
    parser.add_argument(
        "--protoset-out", help="atomically write the complete descriptor set"
    )
    parser.add_argument(
        "--use-reflection",
        action="store_true",
        help="use reflection for missing local symbols",
    )
    parser.add_argument(
        "--proto-path",
        action="append",
        default=list(proto_paths),
        help=(
            "proto import root "
            f"(repeatable; defaults: {', '.join(proto_paths)})"
        ),
    )


def _add_invocation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-d",
        "--data",
        default=argparse.SUPPRESS,
        help="literal input; @path reads a file; - or @- reads stdin",
    )
    parser.add_argument(
        "--input-format",
        choices=_FORMATS,
        default=argparse.SUPPRESS,
        help="request encoding (default: json)",
    )
    parser.add_argument(
        "--output-format",
        choices=_FORMATS,
        default=argparse.SUPPRESS,
        help="response encoding (default: json)",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=argparse.SUPPRESS,
        metavar="NAME=VALUE",
        help="set a field on each raw input message (repeatable)",
    )
    parser.add_argument(
        "--field:file",
        dest="field_file",
        action="append",
        default=argparse.SUPPRESS,
        metavar="NAME=PATH",
        help="read a file (- for stdin) into a bytes field and use raw input",
    )
    parser.add_argument(
        "--input-field",
        default=argparse.SUPPRESS,
        help="bytes field for raw input",
    )
    parser.add_argument(
        "--output-field",
        default=argparse.SUPPRESS,
        help="bytes field for raw output",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=argparse.SUPPRESS,
        help=f"raw stream chunk size in bytes (default: {DEFAULT_CHUNK_SIZE})",
    )
    parser.add_argument(
        "--allow-unknown-fields",
        action="store_true",
        default=argparse.SUPPRESS,
        help="ignore unknown ProtoJSON input fields",
    )
    parser.add_argument(
        "--emit-defaults",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include fields with default values in ProtoJSON output",
    )
    parser.add_argument(
        "--max-input-buffer",
        type=int,
        default=argparse.SUPPRESS,
        help=f"maximum buffered input bytes (default: {DEFAULT_INPUT_BUFFER})",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="unsupported; use --data - or --data @- for streaming stdin",
    )
    parser.add_argument(
        "--wait-for-ready",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "let the RPC wait for readiness until --max-time; local schemas "
            "skip the connection preflight"
        ),
    )


def _add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--plaintext", action="store_true", help="disable TLS"
    )
    transport.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="disable TLS peer verification (unsafe)",
    )
    parser.add_argument(
        "--cacert",
        action="append",
        default=[],
        help="PEM CA bundle (repeatable)",
    )
    parser.add_argument("--cert", help="PEM client certificate")
    parser.add_argument("--key", help="PEM client private key")
    parser.add_argument(
        "--key-password",
        help="client private-key password (may be visible in shell history)",
    )
    parser.add_argument(
        "--servername", help="TLS SNI and verified server identity"
    )
    parser.add_argument("--authority", help="HTTP/2 :authority value")


def _add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=(
            "channel readiness timeout in seconds "
            f"(default: {DEFAULT_CONNECT_TIMEOUT:g})"
        ),
    )
    parser.add_argument(
        "-m",
        "--max-time",
        dest="timeout",
        metavar="SECONDS",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="maximum RPC time in seconds",
    )
    parser.add_argument(
        "--max-recv-message-size",
        type=int,
        help="maximum response message bytes",
    )
    parser.add_argument(
        "--max-send-message-size",
        type=int,
        help="maximum request message bytes",
    )
    parser.add_argument(
        "--compression",
        choices=("none", "gzip", "deflate"),
        default="none",
        help="gRPC message compression (default: none)",
    )
    parser.add_argument(
        "--keepalive-time",
        type=float,
        help="HTTP/2 keepalive interval in seconds",
    )
    parser.add_argument(
        "-A", "--user-agent", help="gRPC primary user-agent value"
    )


def _add_metadata_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        help="shared metadata key:value",
    )
    parser.add_argument(
        "--reflect-header",
        action="append",
        default=[],
        help="reflection-only metadata",
    )


def _add_rpc_metadata_argument(parser: argparse.ArgumentParser) -> None:
    """Add metadata which is meaningful only for method invocation."""
    parser.add_argument(
        "--rpc-header",
        action="append",
        default=[],
        help="application RPC-only metadata",
    )


def _add_diagnostic_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--color",
        choices=COLOR_POLICIES,
        default=argparse.SUPPRESS,
        help="diagnostic color policy (default: auto)",
    )
    parser.add_argument(
        "--diagnostic-format",
        choices=DIAGNOSTIC_FORMATS,
        default=argparse.SUPPRESS,
        help="stderr diagnostic encoding (default: human)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase stderr diagnostics",
    )
    parser.set_defaults(
        show_headers=False,
        show_trailers=False,
        show_status=False,
        format_error=False,
    )


def _add_response_diagnostic_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "-i",
        "--include",
        dest="show_headers",
        action="store_true",
        help="include response metadata on stderr",
    )
    parser.add_argument(
        "--show-trailers", action="store_true", help="show response trailers"
    )
    parser.add_argument(
        "--show-status",
        action="store_true",
        help="show final status and counts",
    )
    parser.add_argument(
        "--format-error",
        action="store_true",
        help="decode structured gRPC errors",
    )
