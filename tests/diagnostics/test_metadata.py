from __future__ import annotations

import io

import pytest

from protodial.presentation.diagnostics.models import (
    DiagnosticEvent,
    RecordingDiagnosticSink,
)
from protodial.presentation.diagnostics.rendering import StderrDiagnosticSink
from protodial.request_metadata import (
    MetadataError,
    parse_metadata,
    parse_metadata_scopes,
)


def test_recording_and_stderr_sinks_are_structured_and_filtered() -> None:
    event = DiagnosticEvent(
        "rpc.started",
        "calling example.Service/Get",
        level=2,
        fields={"target": "localhost:443"},
        metadata=(("Authorization", "Bearer secret"), ("x-visible", "yes")),
    )
    recording = RecordingDiagnosticSink()
    recording.emit(event)
    assert recording.events == [event]

    stream = io.StringIO()
    sink = StderrDiagnosticSink(verbosity=1, stream=stream)
    sink.emit(event)
    assert stream.getvalue() == ""

    sink.verbosity = 2
    sink.emit(event)
    rendered = stream.getvalue()
    assert rendered == (
        "* calling example.Service/Get\n"
        "> request metadata\n"
        "> Authorization: Bearer secret\n"
        "> x-visible: yes\n"
        ">\n"
    )


def test_metadata_scopes_preserve_order_and_values() -> None:
    scopes = parse_metadata_scopes(
        ["Same: shared", "empty:", "data-bin: AP8="],
        ["same: reflection"],
        ["same: rpc", "colon: one:two", "spaces:  intentional "],
    )
    assert scopes.reflection == [
        ("same", "shared"),
        ("empty", ""),
        ("data-bin", b"\x00\xff"),
        ("same", "reflection"),
    ]
    assert scopes.rpc == [
        ("same", "shared"),
        ("empty", ""),
        ("data-bin", b"\x00\xff"),
        ("same", "rpc"),
        ("colon", "one:two"),
        ("spaces", " intentional "),
    ]


@pytest.mark.parametrize(
    "header", ["missing-colon", ": value", "bad key:value", "ümlaut:value"]
)
def test_metadata_rejects_invalid_syntax_and_keys(header: str) -> None:
    with pytest.raises(MetadataError):
        parse_metadata([header])


def test_metadata_preserves_literal_dollar_expressions() -> None:
    raw = "${TOKEN}:$TOKEN:$$TOKEN:$(echo nope)"
    assert parse_metadata([f"authorization:{raw}"]) == [("authorization", raw)]


def test_binary_metadata_requires_strict_base64() -> None:
    assert parse_metadata(["token-bin:AAE="]) == [("token-bin", b"\x00\x01")]
    with pytest.raises(MetadataError, match="token-bin"):
        parse_metadata(["token-bin:not base64!"])
    with pytest.raises(MetadataError, match="token-bin"):
        parse_metadata(["token-bin:${TOKEN}"])


@pytest.mark.parametrize(
    ("header", "codepoint"),
    [
        ("x:value\nnext", "U+000A"),
        ("x:value\tmore", "U+0009"),
        ("x:value\x7f", "U+007F"),
        ("x:café", "U+00E9"),
    ],
)
def test_non_binary_metadata_rejects_non_printable_or_non_ascii_values(
    header: str, codepoint: str
) -> None:
    with pytest.raises(MetadataError) as error:
        parse_metadata([header])

    message = str(error.value)
    assert "metadata 'x'" in message
    assert codepoint in message
    assert "printable ASCII" in message
    assert "'-bin'" in message


def test_non_binary_metadata_accepts_full_printable_ascii_range() -> None:
    value = "".join(chr(codepoint) for codepoint in range(0x21, 0x7F)) + " "
    assert parse_metadata([f"x:{value}"]) == [("x", value)]
