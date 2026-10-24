"""Semantic validation of parsed command-line options."""

from __future__ import annotations

import argparse
import math

from ..commands.models import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_INPUT_BUFFER,
    InvokeOptions,
)
from ..commands.operations import Describe, Invoke, ListSymbols, Operation
from ..formats import MessageFormat

_MAX_MESSAGE_SIZE = 2_147_483_647
_INVOCATION_OPTION_LABELS = {
    "data": "-d/--data",
    "input_format": "--input-format",
    "output_format": "--output-format",
    "field": "--field",
    "field_file": "--field:file",
    "input_field": "--input-field",
    "output_field": "--output-field",
    "chunk_size": "--chunk-size",
    "allow_unknown_fields": "--allow-unknown-fields",
    "emit_defaults": "--emit-defaults",
    "max_input_buffer": "--max-input-buffer",
    "interactive": "--interactive",
    "wait_for_ready": "--wait-for-ready",
}


def validate_invocation_options(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    operation: Operation,
) -> InvokeOptions | None:
    """Validate and build options that apply only to invocations."""
    if not isinstance(operation, Invoke):
        supplied = [
            label
            for name, label in _INVOCATION_OPTION_LABELS.items()
            if hasattr(namespace, name)
        ]
        if supplied:
            parser.error(
                f"{', '.join(supplied)} can only be used when invoking a method"
            )
        return None

    if getattr(namespace, "interactive", False):
        parser.error(
            "--interactive is not supported; use --data - or --data @- for "
            "streaming stdin input"
        )
    options = _build_invoke_options(parser, namespace)
    _validate_invoke_options(parser, options)
    return options


def _parse_field_assignment(
    parser: argparse.ArgumentParser,
    option: str,
    assignment: str,
    *,
    require_value: bool = False,
) -> tuple[str, str]:
    name, separator, value = assignment.partition("=")
    name = name.strip()
    if not separator or not name or (require_value and not value):
        expected = "NAME=PATH" if require_value else "NAME=VALUE"
        parser.error(f"{option} requires {expected}")
    return name, value


def _build_invoke_options(
    parser: argparse.ArgumentParser, namespace: argparse.Namespace
) -> InvokeOptions:
    field_files = getattr(namespace, "field_file", ())
    if len(field_files) > 1:
        parser.error("--field:file may only be specified once")

    input_format = MessageFormat(
        getattr(namespace, "input_format", MessageFormat.JSON)
    )
    data = getattr(namespace, "data", None)
    data_argument = "--data"
    data_option_strings = ("-d", "--data")
    data_option_value = data
    input_field = getattr(namespace, "input_field", None)
    if field_files:
        if data is not None:
            parser.error("--field:file cannot be combined with -d/--data")
        if input_field is not None:
            parser.error("--field:file cannot be combined with --input-field")
        if (
            hasattr(namespace, "input_format")
            and input_format is not MessageFormat.RAW
        ):
            parser.error(
                "--field:file cannot be combined with a non-raw --input-format"
            )
        input_field, path = _parse_field_assignment(
            parser, "--field:file", field_files[0], require_value=True
        )
        input_format = MessageFormat.RAW
        data = f"@{path}"
        data_argument = "--field:file"
        data_option_strings = ("--field:file",)
        data_option_value = field_files[0]

    fields = tuple(
        _parse_field_assignment(parser, "--field", assignment)
        for assignment in getattr(namespace, "field", ())
    )
    return InvokeOptions(
        data=data,
        input_format=input_format,
        output_format=MessageFormat(
            getattr(namespace, "output_format", MessageFormat.JSON)
        ),
        input_field=input_field,
        output_field=getattr(namespace, "output_field", None),
        chunk_size=getattr(namespace, "chunk_size", DEFAULT_CHUNK_SIZE),
        allow_unknown_fields=getattr(namespace, "allow_unknown_fields", False),
        emit_defaults=getattr(namespace, "emit_defaults", False),
        max_input_buffer=getattr(
            namespace, "max_input_buffer", DEFAULT_INPUT_BUFFER
        ),
        max_send_message_size=namespace.max_send_message_size,
        wait_for_ready=getattr(namespace, "wait_for_ready", False),
        fields=fields,
        data_argument=data_argument,
        data_option_strings=data_option_strings,
        data_option_value=data_option_value,
    )


def _validate_invoke_options(
    parser: argparse.ArgumentParser, options: InvokeOptions
) -> None:
    _positive_integer(
        parser, options.chunk_size, "--chunk-size", maximum=_MAX_MESSAGE_SIZE
    )
    if options.max_input_buffer is not None:
        _positive_integer(
            parser,
            options.max_input_buffer,
            "--max-input-buffer",
            maximum=_MAX_MESSAGE_SIZE,
        )
    if options.input_field and options.input_format is not MessageFormat.RAW:
        parser.error("--input-field requires --input-format raw")
    if options.fields and options.input_format is not MessageFormat.RAW:
        parser.error("--field requires raw input or --field:file")
    if options.fields and options.data is None:
        parser.error("--field requires -d/--data or --field:file")
    if options.output_field and options.output_format is not MessageFormat.RAW:
        parser.error("--output-field requires --output-format raw")


def _positive_finite(
    parser: argparse.ArgumentParser, value: float, option: str
) -> None:
    if not math.isfinite(value) or value <= 0:
        parser.error(f"{option} must be finite and positive")


def _positive_integer(
    parser: argparse.ArgumentParser,
    value: int,
    option: str,
    *,
    maximum: int | None = None,
) -> None:
    if value <= 0:
        parser.error(f"{option} must be positive")
    if maximum is not None and value > maximum:
        parser.error(f"{option} must not exceed {maximum}")


def validate_common_options(
    parser: argparse.ArgumentParser, namespace: argparse.Namespace
) -> None:
    """Validate options shared by invocation and discovery commands."""
    if bool(namespace.cert) != bool(namespace.key):
        parser.error("--cert and --key must be used together")
    if namespace.key_password is not None and not namespace.key:
        parser.error("--key-password requires --key")
    if namespace.servername is not None and not namespace.servername.strip():
        parser.error("--servername must not be empty")
    if namespace.authority is not None and not namespace.authority.strip():
        parser.error("--authority must not be empty")
    if namespace.user_agent is not None and (
        not namespace.user_agent
        or any(ord(character) < 32 for character in namespace.user_agent)
    ):
        parser.error(
            "--user-agent must be non-empty and contain no control characters"
        )

    _positive_finite(parser, namespace.timeout, "--max-time")
    _positive_finite(parser, namespace.connect_timeout, "--connect-timeout")
    if namespace.keepalive_time is not None:
        _positive_finite(parser, namespace.keepalive_time, "--keepalive-time")
        if namespace.keepalive_time < 0.001:
            parser.error("--keepalive-time must be at least 0.001 seconds")
    for name in ("max_recv_message_size", "max_send_message_size"):
        value = getattr(namespace, name)
        if value is not None:
            _positive_integer(
                parser,
                value,
                f"--{name.replace('_', '-')}",
                maximum=_MAX_MESSAGE_SIZE,
            )
    if hasattr(namespace, "template_depth"):
        _positive_integer(parser, namespace.template_depth, "--template-depth")
    if namespace.use_reflection and not (namespace.proto or namespace.protoset):
        parser.error(
            "--use-reflection requires a local --proto or --protoset source"
        )


def validate_operation_options(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    operation: Operation,
) -> None:
    """Reject options that do not apply to the resolved operation."""
    if namespace.show_reflection and not isinstance(operation, ListSymbols):
        parser.error("--show-reflection can only be used with list")
    if namespace.get_template and not isinstance(operation, Describe):
        parser.error("--template can only be used with describe")
    if hasattr(namespace, "template_depth") and not namespace.get_template:
        parser.error("--template-depth requires --template")

    focused_diagnostics = (
        namespace.show_headers
        or namespace.show_trailers
        or namespace.show_status
        or namespace.format_error
    )
    if focused_diagnostics and not isinstance(operation, Invoke):
        parser.error(
            "response diagnostic options can only be used when invoking a "
            "method"
        )
