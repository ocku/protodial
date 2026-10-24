from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from google.protobuf import descriptor_pool, empty_pb2

from protodial.argument_parser import parse_arguments, parse_target
from protodial.errors import CliError
from protodial.message_io import (
    iter_json_values,
    iter_text_requests,
    render_response,
)
from protodial.presentation.diagnostics.models import DiagnosticEvent
from protodial.presentation.diagnostics.rendering import (
    _display_width,
    render_human,
)
from protodial.presentation.rpc import (
    render_metadata,
    render_rpc_diagnostics,
    render_rpc_error,
)
from protodial.rich_status import (
    DecodedStatusDetail,
    RichStatus,
    render_rich_status,
)


class _ChunkSource:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    def read(self, _size: int = -1) -> bytes:
        raise AssertionError("incremental JSON input should prefer read1")

    def read1(self, _size: int = -1) -> bytes:
        return next(self._chunks, b"")


def _options(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "allow_unknown_fields": False,
        "emit_defaults": False,
        "max_input_buffer": None,
        "output_format": "text",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_incremental_json_does_not_emit_numeric_prefix() -> None:
    source = _ChunkSource([b"1", b"2 ", b"3", b".5", b"e2\n"])

    assert [value for value, _raw in iter_json_values(source, _options())] == [
        12,
        350.0,
    ]


def test_streaming_textproto_round_trips_empty_messages() -> None:
    options = _options()
    empty = empty_pb2.Empty()
    payload = b"".join(
        render_response(empty, options, streaming=True) for _ in range(2)
    )

    requests = list(
        iter_text_requests(
            io.BytesIO(payload),
            True,
            empty_pb2.Empty,
            descriptor_pool.Default(),
            options,
        )
    )

    assert payload == b"\n---\n\n---\n"
    assert requests == [empty_pb2.Empty(), empty_pb2.Empty()]


@pytest.mark.parametrize(
    "target",
    [
        "grpc://[::1/example.Service/Call",
        "grpcs://localhost:not-a-port/example.Service/Call",
        "grpc://:443/example.Service/Call",
        "grpc://localhost:/example.Service/Call",
    ],
)
def test_malformed_grpc_urls_are_normalized_to_cli_errors(target: str) -> None:
    with pytest.raises(CliError, match="target URL"):
        parse_target(target)


def test_url_parsing_normalizes_and_preserves_resolvers() -> None:
    parsed = parse_target("GRPC://Example.COM:50051/example.Service/Call")
    assert (parsed.channel, parsed.method, parsed.transport) == (
        "Example.COM:50051",
        "example.Service/Call",
        "plaintext",
    )
    parsed = parse_target("DNS+SRV:///service.example:443")
    assert (parsed.channel, parsed.method, parsed.transport) == (
        "DNS+SRV:///service.example:443",
        None,
        None,
    )
    assert parse_target("dns:/service.example:443").channel == (
        "dns:/service.example:443"
    )


@pytest.mark.parametrize("target", ["::::", "::1:not-a-port", "localhost:"])
def test_malformed_bare_targets_are_rejected_early(target: str) -> None:
    with pytest.raises(CliError):
        parse_target(target)


def test_standard_url_parser_value_errors_are_normalized() -> None:
    with pytest.raises(CliError, match="invalid target URL"):
        parse_target("grpc://example.com：443/example.Service/Call")


def test_malformed_grpc_url_argument_does_not_escape_as_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        parse_arguments(["grpc://[::1/example.Service/Call"])

    assert raised.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_rpc_renderers_preserve_metadata_values() -> None:
    summary = SimpleNamespace(
        initial_metadata=[
            ("authorization", "Bearer secret"),
            ("ordinary", "visible"),
        ],
        trailing_metadata=[],
        code=SimpleNamespace(name="OK"),
        details="",
        sent_messages=1,
        received_messages=1,
        elapsed_seconds=0.01,
    )

    assert render_metadata(summary.initial_metadata) == [
        "authorization: Bearer secret",
        "ordinary: visible",
    ]
    rendered = render_rpc_diagnostics(
        summary, show_headers=True, show_trailers=False, show_status=False
    )
    assert "authorization: Bearer secret" in rendered


def test_public_metadata_renderer_wraps_by_cells_and_preserves_duplicates() -> (
    None
):
    metadata = [
        ("same\x1b[2J界", "first界 value " * 12),
        ("same\x1b[2J界", "second界 value " * 12),
        ("k" * 37, "界 tail"),
    ]

    lines = render_metadata(metadata, width=40)

    assert all(_display_width(line) <= 40 for line in lines)
    assert sum(line.startswith("  ") for line in lines) >= 2
    rendered = "\n".join(lines)
    assert rendered.count(r"same\x1b[2J界:") == 2
    assert rendered.index("first") < rendered.index("second")
    assert "\x1b" not in rendered


def test_rpc_summary_uses_curl_like_wire_markers() -> None:
    summary = SimpleNamespace(
        initial_metadata=[("example-server", "hello")],
        trailing_metadata=[("request-id", "example-123")],
        code=SimpleNamespace(name="OK"),
        details="",
        sent_messages=1,
        received_messages=1,
        elapsed_seconds=0.0012,
    )

    assert render_rpc_diagnostics(
        summary,
        show_headers=True,
        show_trailers=True,
        show_status=True,
    ) == (
        "< response headers\n"
        "< example-server: hello\n"
        "<\n"
        "< response trailers\n"
        "< request-id: example-123\n"
        "<\n"
        "* OK in 1.2 ms; sent 1 message; received 1 message"
    )


def test_non_ok_rpc_summary_is_a_status_line_with_a_reason() -> None:
    summary = SimpleNamespace(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="INVALID_ARGUMENT", value=(3, "unused")),
        details="validation failed",
        sent_messages=1,
        received_messages=0,
        elapsed_seconds=0.0012,
    )

    assert render_rpc_diagnostics(
        summary,
        show_headers=False,
        show_trailers=False,
        show_status=True,
    ) == (
        "* INVALID_ARGUMENT (3) in 1.2 ms; sent 1 message; "
        "received 0 messages\n"
        "  reason: validation failed"
    )


def test_ok_rpc_summary_does_not_show_numeric_zero() -> None:
    summary = SimpleNamespace(
        initial_metadata=[],
        trailing_metadata=[],
        code=SimpleNamespace(name="OK", value=(0, "unused")),
        details="",
        sent_messages=1,
        received_messages=1,
        elapsed_seconds=0.001,
    )

    rendered = render_rpc_diagnostics(
        summary,
        show_headers=False,
        show_trailers=False,
        show_status=True,
    )

    assert rendered == "* OK in 1 ms; sent 1 message; received 1 message"
    assert "OK (0)" not in rendered


def test_rpc_aggregator_omits_empty_metadata_sections() -> None:
    summary = SimpleNamespace(
        initial_metadata=[],
        trailing_metadata=[("blank", " \t"), (" ", "visible")],
        code=SimpleNamespace(name="OK", value=(0, "unused")),
        details="",
        sent_messages=1,
        received_messages=1,
        elapsed_seconds=0.001,
    )

    assert (
        render_rpc_diagnostics(
            summary,
            show_headers=True,
            show_trailers=True,
            show_status=True,
        )
        == "* OK in 1 ms; sent 1 message; received 1 message"
    )
    assert (
        render_rpc_diagnostics(
            summary,
            show_headers=True,
            show_trailers=True,
            show_status=False,
        )
        == ""
    )


def test_server_control_characters_are_escaped_in_rpc_diagnostics() -> None:
    summary = SimpleNamespace(
        initial_metadata=[("x-name\nforged", "value\x1b[31m\rforged")],
        trailing_metadata=[],
        code=SimpleNamespace(name="INTERNAL\nforged"),
        details="failure\nStatus: OK\x1b]0;owned\x07\u202eforged",
        sent_messages=1,
        received_messages=0,
        elapsed_seconds=0.25,
    )
    error = SimpleNamespace(
        code=lambda: SimpleNamespace(
            name="INTERNAL\rforged", value=(13, "unused")
        ),
        details=lambda: "bad\nforged\x1b[2J",
    )

    rendered = render_rpc_diagnostics(
        summary, show_headers=True, show_trailers=False, show_status=True
    )
    rendered_error = render_rpc_error(error)

    assert render_metadata(summary.initial_metadata) == [
        r"x-name\nforged: value\x1b[31m\rforged"
    ]
    assert (
        "* INTERNAL\\nforged in 250 ms; sent 1 message; "
        "received 0 messages" in rendered
    )
    assert "status: INTERNAL\\nforged" not in rendered
    assert "INTERNAL\\nforged" in rendered
    assert "failure\\nStatus: OK\\x1b]0;owned\\x07\\u202eforged" in rendered
    assert rendered_error == (
        "protodial: (E4103) INTERNAL\\rforged (13)\n"
        "  reason: bad\\nforged\\x1b[2J"
    )
    assert "\x1b" not in rendered
    assert "\x1b" not in rendered_error


def test_server_controls_are_escaped_in_diagnostics() -> None:
    status = RichStatus(
        13,
        "inner\nforged\x1b[31m",
        (
            DecodedStatusDetail(
                "type.example/Bad\rType",
                "Bad\rType",
                b"payload",
                warning="warning\nforged",
            ),
        ),
        ("outer\twarning\x7f",),
    )
    event = DiagnosticEvent(
        "rpc\nforged",
        "message\x1b[2J",
        fields={"field\rname": "value\tforged"},
        metadata=(("meta\nname", "meta\x07value"),),
    )

    rich = render_rich_status(
        status,
        transport_code=13,
        transport_message="transport\nforged\x1b]0;owned\x07",
    )
    diagnostic = render_human(event)

    assert "transport\\nforged\\x1b]0;owned\\x07" in rich
    assert "inner\\nforged\\x1b[31m" in rich
    assert "Bad\\rType" in rich
    assert "warning\\nforged" in rich
    assert "outer\\twarning\\x7f" in rich
    assert "message\\x1b[2J" in diagnostic
    assert "meta\\nname: meta\\x07value" in diagnostic
    assert "\x1b" not in rich
    assert "\x1b" not in diagnostic
