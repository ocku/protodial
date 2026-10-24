"""Resolution of command operands and transport mode."""

from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Literal

from ..commands.models import ParsedTarget
from ..commands.operations import Describe, Invoke, ListSymbols, Operation
from ..errors import CliError
from .target import (
    parse_discovery_target,
    parse_target,
    validate_insecure_target,
)

TransportMode = Literal["verified", "insecure", "plaintext"]


def resolve_command(
    parser: argparse.ArgumentParser, namespace: argparse.Namespace
) -> tuple[ParsedTarget, Operation]:
    """Resolve operands from one explicitly selected subcommand."""
    if namespace.command_name == "list":
        return _resolve_list(parser, namespace.target, namespace.service)
    if namespace.command_name == "describe":
        return _resolve_describe(parser, namespace.target, namespace.symbol)
    if namespace.command_name == "invoke":
        return _resolve_invocation(parser, namespace.target, namespace.method)
    parser.error("a command is required")


def _resolve_list(
    parser: argparse.ArgumentParser,
    target_input: str,
    service: str | None,
) -> tuple[ParsedTarget, ListSymbols]:
    target, path_service = _parse_discovery_target(parser, target_input)
    if path_service is not None and "/" in path_service:
        parser.error("list URL path must identify a service")
    if service is not None and path_service is not None:
        parser.error("service is already included in the list URL")
    return target, ListSymbols(service or path_service)


def _resolve_describe(
    parser: argparse.ArgumentParser,
    target_input: str,
    symbol: str | None,
) -> tuple[ParsedTarget, Describe]:
    target, path_symbol = _parse_discovery_target(parser, target_input)
    if symbol is not None and path_symbol is not None:
        parser.error("symbol is already included in the describe URL")
    resolved_symbol = symbol or path_symbol
    if resolved_symbol is None:
        parser.error("describe requires a SYMBOL operand or URL path")
    return target, Describe(resolved_symbol)


def _resolve_invocation(
    parser: argparse.ArgumentParser,
    target_input: str,
    operand_method: str | None,
) -> tuple[ParsedTarget, Invoke]:
    target = _parse_target(parser, target_input)

    if target.method is not None and operand_method is not None:
        parser.error("method is already included in the invocation URL")
    method = target.method or operand_method
    if method is None:
        parser.error(
            "invocation requires /SERVICE/METHOD in a grpc[s] URL, or "
            "--method SERVICE/METHOD for a native gRPC target"
        )
    method_parts = method.split("/")
    if len(method_parts) != 2 or not all(method_parts):
        parser.error("method must have the form SERVICE/METHOD")
    return replace(target, method=method), Invoke(method)


def _parse_target(
    parser: argparse.ArgumentParser, target_input: str
) -> ParsedTarget:
    try:
        return parse_target(target_input)
    except CliError as error:
        parser.error(str(error))


def _parse_discovery_target(
    parser: argparse.ArgumentParser, target_input: str
) -> tuple[ParsedTarget, str | None]:
    try:
        return parse_discovery_target(target_input)
    except CliError as error:
        parser.error(str(error))


def _tls_credentials_supplied(namespace: argparse.Namespace) -> bool:
    return any(
        (
            namespace.cacert,
            namespace.cert,
            namespace.key,
            namespace.key_password is not None,
            namespace.servername,
        )
    )


def _requested_transport_mode(namespace: argparse.Namespace) -> TransportMode:
    if namespace.insecure:
        return "insecure"
    if namespace.plaintext:
        return "plaintext"
    return "verified"


def _apply_target_transport(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    target: ParsedTarget,
    requested_mode: TransportMode,
) -> TransportMode:
    if target.transport == "plaintext":
        if namespace.insecure or _tls_credentials_supplied(namespace):
            parser.error("grpc:// cannot be combined with TLS-only options")
        return "plaintext"
    if target.transport == "tls" and requested_mode == "plaintext":
        parser.error("grpcs:// cannot be combined with --plaintext")
    return requested_mode


def _validate_transport_options(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    target: ParsedTarget,
    mode: TransportMode,
) -> None:
    if mode == "plaintext" and _tls_credentials_supplied(namespace):
        parser.error("TLS credential options cannot be used with --plaintext")
    if mode == "insecure" and namespace.cacert:
        parser.error("--cacert cannot be used with --insecure")
    if mode != "insecure":
        return
    try:
        validate_insecure_target(target)
    except CliError as error:
        parser.error(str(error))


def resolve_transport_mode(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    target: ParsedTarget,
) -> TransportMode:
    """Resolve and validate the selected transport security mode."""
    requested_mode = _requested_transport_mode(namespace)
    mode = _apply_target_transport(parser, namespace, target, requested_mode)
    _validate_transport_options(parser, namespace, target, mode)
    return mode
