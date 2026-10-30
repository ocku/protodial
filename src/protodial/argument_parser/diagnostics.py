"""Resolve diagnostic policy and locate invalid command-line arguments."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from ..presentation.diagnostics.models import DiagnosticSpan
from .construction import (
    COLOR_POLICIES,
    DIAGNOSTIC_FORMATS,
    value_option_names,
)

_DIAGNOSTIC_FORMAT_OPTION = "--diagnostic-format"
_COLOR_OPTION = "--color"
_PRESENTATION_OPTIONS = frozenset({_DIAGNOSTIC_FORMAT_OPTION, _COLOR_OPTION})
_DIAGNOSTIC_FORMAT_ENV = "PROTODIAL_DIAGNOSTIC_FORMAT"
_ARGPARSE_VALUE_ERROR = re.compile(
    r"argument (?P<option>-{1,2}[^ :]+): .*?'(?P<value>[^']*)'"
)
_ARGPARSE_UNRECOGNIZED_ERROR = re.compile(
    r"unrecognized arguments?: (?P<value>\S+)"
)
_ARGPARSE_ERROR_LINE = re.compile(
    r"^protodial(?: [^:\r\n]+)?: error: (?P<message>.*)$"
)


@dataclass(frozen=True, slots=True)
class _BootstrapValue:
    value: str
    source: str
    text: str
    start: int
    end: int


def bootstrap_policy(
    argv: Sequence[str], environ: Mapping[str, str]
) -> tuple[str, str, str | None]:
    """Resolve exact presentation options before the full parser runs."""
    diagnostic_format, color, error, _span = bootstrap_policy_details(
        argv, environ
    )
    return diagnostic_format, color, error


def _is_presentation_option(token: str) -> bool:
    return token in _PRESENTATION_OPTIONS or any(
        token.startswith(f"{option}=") for option in _PRESENTATION_OPTIONS
    )


def _bootstrap_value(
    argv: Sequence[str], index: int, option: str
) -> _BootstrapValue | None:
    token = argv[index]
    if token == option:
        value_index = index + 1
        if value_index >= len(argv):
            return None
        value = argv[value_index]
        if value == "--" or value.startswith("-"):
            return None
        return _BootstrapValue(
            value,
            f"argument {value_index + 1}",
            value,
            0,
            len(value),
        )

    prefix = f"{option}="
    if not token.startswith(prefix):
        return None
    return _BootstrapValue(
        token[len(prefix) :],
        f"argument {index + 1}",
        token,
        len(prefix),
        len(token),
    )


def _bootstrap_option_indices(argv: Sequence[str]) -> Iterator[int]:
    """Yield visible option positions before the full argparse pass."""
    value_options = value_option_names() - _PRESENTATION_OPTIONS
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return
        yield index

        if token in value_options and index + 1 < len(argv):
            following = argv[index + 1]
            if following == "--":
                return
            index += 1 if _is_presentation_option(following) else 2
        elif (
            token in _PRESENTATION_OPTIONS
            and _bootstrap_value(argv, index, token) is not None
        ):
            index += 2
        else:
            index += 1


def _invalid_diagnostic_format(
    value: _BootstrapValue,
) -> tuple[str, str, str, DiagnosticSpan]:
    return (
        "human",
        "never",
        f"diagnostic format must be one of: {', '.join(DIAGNOSTIC_FORMATS)}",
        DiagnosticSpan(
            value.source,
            text=value.text,
            start=value.start,
            end=value.end,
            label="invalid diagnostic format",
        ),
    )


def bootstrap_policy_details(
    argv: Sequence[str], environ: Mapping[str, str]
) -> tuple[str, str, str | None, DiagnosticSpan | None]:
    """Resolve policy and retain the source of an invalid format value."""
    diagnostic_format: str | None = None
    color = "auto"
    for index in _bootstrap_option_indices(argv):
        format_value = _bootstrap_value(argv, index, _DIAGNOSTIC_FORMAT_OPTION)
        if format_value is not None:
            if format_value.value not in DIAGNOSTIC_FORMATS:
                return _invalid_diagnostic_format(format_value)
            diagnostic_format = format_value.value
            continue

        color_value = _bootstrap_value(argv, index, _COLOR_OPTION)
        if color_value is not None:
            color = color_value.value

    if diagnostic_format is None:
        diagnostic_format = environ.get(_DIAGNOSTIC_FORMAT_ENV, "human")
    if diagnostic_format not in DIAGNOSTIC_FORMATS:
        return _invalid_diagnostic_format(
            _BootstrapValue(
                diagnostic_format,
                f"environment variable {_DIAGNOSTIC_FORMAT_ENV}",
                diagnostic_format,
                0,
                len(diagnostic_format),
            )
        )
    # Let argparse report an invalid color using the selected diagnostic format.
    if color not in COLOR_POLICIES:
        color = "auto"
    return diagnostic_format, color, None, None


def parser_message(captured: str) -> str:
    """Extract argparse error prose from its captured stderr output."""
    for line in captured.splitlines():
        matched = _ARGPARSE_ERROR_LINE.fullmatch(line)
        if matched is not None:
            return matched.group("message")
    return captured.strip() or "command-line arguments are invalid"


def _invalid_option_value_span(
    argv: Sequence[str], option: str, value: str
) -> DiagnosticSpan | None:
    """Locate the first spelling of an option with the rejected value."""
    prefix = f"{option}="
    for index, token in enumerate(argv):
        if (
            token == option
            and index + 1 < len(argv)
            and argv[index + 1] == value
        ):
            return DiagnosticSpan(
                f"argument {index + 2}",
                text=value,
                start=0,
                end=len(value),
                label="invalid value",
            )
        if token.startswith(prefix) and token[len(prefix) :] == value:
            return DiagnosticSpan(
                f"argument {index + 1}",
                text=token,
                start=len(prefix),
                end=len(token),
                label="invalid value",
            )
    return None


def _invalid_argument_span(
    argv: Sequence[str], message: str, wanted: str | None
) -> DiagnosticSpan | None:
    for position, token in enumerate(argv, 1):
        if wanted != token and (not token or token not in message):
            continue
        start = 0
        if "=" in token:
            option, value = token.split("=", 1)
            if option in message and value in message:
                start = len(option) + 1
        return DiagnosticSpan(
            f"argument {position}",
            text=token,
            start=start,
            end=len(token),
            label="invalid argument",
        )
    return None


def argument_span(
    argv: Sequence[str], message: str
) -> tuple[DiagnosticSpan, ...]:
    """Locate argparse's offending value without reconstructing shell input."""
    option_match = _ARGPARSE_VALUE_ERROR.search(message)
    if option_match is not None:
        # argparse rejects the first invalid occurrence it encounters. Match
        # the rejected value rather than assuming the final spelling won.
        span = _invalid_option_value_span(
            argv,
            option_match.group("option"),
            option_match.group("value"),
        )
        if span is not None:
            return (span,)

    unrecognized = _ARGPARSE_UNRECOGNIZED_ERROR.search(message)
    wanted = unrecognized.group("value") if unrecognized is not None else None
    span = _invalid_argument_span(argv, message, wanted)
    return (span,) if span is not None else ()
