from __future__ import annotations

import io
import re

import pytest
from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    duration_pb2,
    json_format,
)
from google.rpc import error_details_pb2

from protodial.presentation._graphemes import grapheme_boundaries
from protodial.presentation._truncation import PrefixSearchMetrics
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
from protodial.rich_status import (
    decode_status_details,
    render_rich_status,
    write_rich_status,
)
from protodial.rich_status import decoding as status_decoding
from protodial.rich_status.rendering import _truncate_rendered


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


def _custom_pool_and_payload() -> tuple[descriptor_pool.DescriptorPool, bytes]:
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="example/detail.proto",
        package="example",
        syntax="proto3",
    )
    message = file_proto.message_type.add(name="ValidationDetail")
    message.field.add(
        name="reason",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("example.ValidationDetail")
    detail = status_decoding.message_class(descriptor)(reason="bad name")
    return pool, detail.SerializeToString()


def _status_bytes(
    *details: tuple[str, bytes], code: int = 3, message: str = "invalid"
) -> bytes:
    status = status_decoding.status_message_class()(code=code, message=message)
    for type_url, payload in details:
        packed = status.details.add()
        packed.type_url = type_url
        packed.value = payload
    return status.SerializeToString()


def test_status_decodes_custom_any_from_active_pool_and_preserves_order() -> (
    None
):
    pool, payload = _custom_pool_and_payload()
    raw = _status_bytes(
        ("type.googleapis.com/example.ValidationDetail", payload),
        ("custom.invalid/unknown.Type", b"\x01\x02"),
    )
    decoded = decode_status_details(
        [("other", "value"), ("grpc-status-details-bin", raw)],
        pool,
        transport_code=3,
        transport_message="invalid",
    )
    assert decoded is not None
    assert decoded.code == 3
    assert decoded.message == "invalid"
    assert decoded.details[0].known
    custom_message = decoded.details[0].message
    assert custom_message is not None
    assert custom_message.reason == "bad name"
    assert not decoded.details[1].known
    assert decoded.details[1].payload == b"\x01\x02"

    rendered = render_rich_status(
        decoded, transport_code=3, transport_message="invalid"
    )
    assert rendered.index('reason: "bad name"') < rendered.index(
        "protodial: (E4103) INVALID_ARGUMENT (3)"
    )
    assert "  reason: invalid" in rendered
    assert "example.ValidationDetail" in rendered
    assert 'reason: "bad name"' in rendered
    assert "base64:AQI=" in rendered
    assert "unknown message type" in rendered


def test_custom_bad_request_short_name_does_not_trigger_google_renderer() -> (
    None
):
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="acme_bad_request.proto", package="acme", syntax="proto3"
    )
    message = file_proto.message_type.add(name="BadRequest")
    message.field.add(
        name="custom_value",
        number=1,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("acme.BadRequest")
    custom = status_decoding.message_class(descriptor)(
        custom_value="MUST_SURVIVE"
    )
    detail = status_decoding.DecodedStatusDetail(
        "type.googleapis.com/acme.BadRequest",
        "acme.BadRequest",
        custom.SerializeToString(),
        message=custom,
    )

    rendered = render_rich_status(
        status_decoding.RichStatus(3, "invalid", (detail,)),
        transport_code=3,
    )

    assert "type.googleapis.com/acme.BadRequest:" in rendered
    assert "custom_value" in rendered
    assert "MUST_SURVIVE" in rendered


def test_malformed_status_and_mismatch_are_warnings_not_exceptions() -> None:
    malformed = decode_status_details(b"\xff")
    assert malformed is not None
    assert malformed.decoded is False
    assert "could not decode" in render_rich_status(
        malformed, transport_code=13, transport_message="primary failure"
    )
    assert "DecodeError" not in render_rich_status(
        malformed, transport_code=13, transport_message="primary failure"
    )
    assert "primary failure" in render_rich_status(
        malformed, transport_code=13, transport_message="primary failure"
    )

    mismatch = decode_status_details(
        _status_bytes(code=3, message="inner"),
        transport_code=13,
        transport_message="outer",
    )
    assert mismatch is not None
    assert any(
        "`outer` differ from rich status message `inner`" in warning
        for warning in mismatch.warnings
    )


def _decoded_detail(message: object) -> status_decoding.DecodedStatusDetail:
    descriptor = message.DESCRIPTOR
    return status_decoding.DecodedStatusDetail(
        f"type.googleapis.com/{descriptor.full_name}",
        descriptor.full_name,
        message.SerializeToString(),
        message=message,
    )


def test_all_well_known_details_have_semantic_human_sections() -> None:
    details = (
        _decoded_detail(
            error_details_pb2.BadRequest(
                field_violations=[
                    error_details_pb2.BadRequest.FieldViolation(
                        field="request.email", description="must be valid"
                    )
                ]
            )
        ),
        _decoded_detail(
            error_details_pb2.ErrorInfo(
                reason="TOKEN_EXPIRED",
                domain="auth.example",
                metadata={"ordinary": "visible"},
            )
        ),
        _decoded_detail(
            error_details_pb2.RetryInfo(
                retry_delay=duration_pb2.Duration(nanos=250_000_000)
            )
        ),
        _decoded_detail(
            error_details_pb2.PreconditionFailure(
                violations=[
                    error_details_pb2.PreconditionFailure.Violation(
                        type="STATE", subject="users/1", description="locked"
                    )
                ]
            )
        ),
        _decoded_detail(
            error_details_pb2.QuotaFailure(
                violations=[
                    error_details_pb2.QuotaFailure.Violation(
                        subject="projects/1", description="daily limit"
                    )
                ]
            )
        ),
        _decoded_detail(
            error_details_pb2.ResourceInfo(
                resource_type="example.User",
                resource_name="users/1",
                owner="teams/identity",
                description="user is unavailable",
            )
        ),
        _decoded_detail(
            error_details_pb2.RequestInfo(
                request_id="request-7", serving_data="cluster-a"
            )
        ),
        _decoded_detail(
            error_details_pb2.DebugInfo(
                stack_entries=["service.py:20"], detail="server trace"
            )
        ),
        _decoded_detail(
            error_details_pb2.Help(
                links=[
                    error_details_pb2.Help.Link(
                        description="API guide", url="https://example.test/api"
                    )
                ]
            )
        ),
        _decoded_detail(
            error_details_pb2.LocalizedMessage(
                locale="sv-SE", message="Ogiltig begäran"
            )
        ),
    )
    status = status_decoding.RichStatus(3, "invalid", details)

    rendered = render_rich_status(status, transport_code=3)

    for expected in (
        "request.email: must be valid",
        "error information:",
        "retry after 250 ms",
        "precondition violations:",
        "quota violations:",
        "resource information:",
        "request information:",
        "debug information:",
        "help:",
        "localized message (sv-SE):",
    ):
        assert expected in rendered
    assert "`STATE` on `users/1`: locked" in rendered
    assert "`example.User` resource `users/1`" in rendered
    assert "request `request-7` served by `cluster-a`" in rendered
    assert "resource_type:" not in rendered


def test_empty_details_are_suppressed_with_bad_request_fallback() -> None:
    empty_details = tuple(
        _decoded_detail(message)
        for message in (
            error_details_pb2.BadRequest(
                field_violations=[error_details_pb2.BadRequest.FieldViolation()]
            ),
            error_details_pb2.ErrorInfo(),
            error_details_pb2.RetryInfo(),
            error_details_pb2.PreconditionFailure(
                violations=[error_details_pb2.PreconditionFailure.Violation()]
            ),
            error_details_pb2.QuotaFailure(
                violations=[error_details_pb2.QuotaFailure.Violation()]
            ),
            error_details_pb2.ResourceInfo(description="  "),
            error_details_pb2.RequestInfo(),
            error_details_pb2.DebugInfo(stack_entries=["\t"]),
            error_details_pb2.Help(links=[error_details_pb2.Help.Link()]),
            error_details_pb2.LocalizedMessage(locale="  ", message="\t"),
        )
    )
    warning = status_decoding.DecodedStatusDetail(
        "type.googleapis.com/google.rpc.BadRequest",
        "google.rpc.BadRequest",
        b"",
        warning="empty detail could not be decoded",
    )
    status = status_decoding.RichStatus(3, "invalid", (*empty_details, warning))

    rendered = render_rich_status(status, transport_code=3)

    assert "validation error details were empty" not in rendered
    assert "request: invalid" in rendered
    assert "error information:" not in rendered
    assert "precondition violations:" not in rendered
    assert "quota violations:" not in rendered
    assert "resource information:" not in rendered
    assert "request information:" not in rendered
    assert "debug information:" not in rendered
    assert "localized message" not in rendered
    assert "empty detail could not be decoded" in rendered
    assert all(line.strip() for line in rendered.splitlines())


def test_rich_renderer_preserves_error_info_metadata() -> None:
    detail = _decoded_detail(
        error_details_pb2.ErrorInfo(
            reason="DENIED",
            domain="auth.example",
            metadata={
                "authorization": "Bearer secret",
                "ordinary": "visible",
            },
        )
    )
    status = status_decoding.RichStatus(7, "denied", (detail,))

    rendered = render_rich_status(status, transport_code=7)

    assert "authorization: Bearer secret" in rendered
    assert "ordinary: visible" in rendered


def test_rich_status_does_not_hide_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStatus:
        def __init__(self) -> None:
            raise RuntimeError("status constructor defect")

    monkeypatch.setattr(
        status_decoding, "status_message_class", lambda: BrokenStatus
    )
    with pytest.raises(RuntimeError, match="status constructor defect"):
        decode_status_details(b"payload")

    decoded = status_decoding.RichStatus(
        3,
        "bad",
        (
            status_decoding.DecodedStatusDetail(
                "type.googleapis.com/google.protobuf.Empty",
                "google.protobuf.Empty",
                b"",
                message=descriptor_pb2.FileDescriptorProto(),
            ),
        ),
    )

    def fail_to_dict(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("JSON rendering defect")

    monkeypatch.setattr(json_format, "MessageToDict", fail_to_dict)
    with pytest.raises(RuntimeError, match="JSON rendering defect"):
        render_rich_status(decoded, message_format="json")

    def type_error(*_args: object, **_kwargs: object) -> object:
        raise TypeError("unexpected conversion defect")

    monkeypatch.setattr(json_format, "MessageToDict", type_error)
    with pytest.raises(TypeError, match="unexpected conversion defect"):
        render_rich_status(decoded, message_format="json")


def test_rich_status_truncation_is_bounded_and_counts_original_characters() -> (
    None
):
    decoded = decode_status_details(_status_bytes(message="x" * 200))
    assert decoded is not None
    full = render_rich_status(decoded)

    limited = render_rich_status(decoded, max_rendered_chars=80)
    assert len(limited) == 80
    marker_start = limited.index("\n  note: rich status truncated;")
    match = re.search(r"(\d+) characters omitted$", limited)
    assert match is not None
    assert int(match.group(1)) == len(full) - marker_start

    assert len(render_rich_status(decoded, max_rendered_chars=1)) == 1
    with pytest.raises(ValueError, match="greater than zero"):
        render_rich_status(decoded, max_rendered_chars=0)
    with pytest.raises(ValueError, match="greater than zero"):
        render_rich_status(decoded, max_rendered_chars=-1)


def test_rich_status_limit_is_utf8_bytes_without_details() -> None:
    rendered = render_rich_status(
        None,
        transport_code=3,
        transport_message="界" * 100,
        max_rendered_chars=80,
    )

    assert len(rendered.encode("utf-8")) <= 80
    assert "rich status truncated" in rendered
    assert "\ufffd" not in rendered


@pytest.mark.parametrize(
    "glyph",
    [
        "\U0001f1f8\U0001f1ea",
        "1\ufe0f\u20e3",
        "\u0915\u093f",
        "e\u0301",
        "\U0001f469\u200d\U0001f4bb",
        "\U0001f44d\U0001f3fd",
    ],
)
def test_standalone_rich_status_truncates_at_grapheme_boundaries(
    glyph: str,
) -> None:
    status = status_decoding.RichStatus(3, glyph * 40, ())
    full = render_rich_status(status, transport_code=3)
    boundaries = set(grapheme_boundaries(full))
    observed_truncation = False
    for limit in range(64, min(220, len(full.encode("utf-8")))):
        truncated = render_rich_status(
            status, transport_code=3, max_rendered_chars=limit
        )
        marker = "\n  note: rich status truncated;"
        if marker not in truncated:
            continue
        observed_truncation = True
        prefix = truncated.split(marker, 1)[0]
        assert len(prefix) in boundaries
        assert len(truncated.encode("utf-8")) <= limit
    assert observed_truncation


def test_standalone_truncation_indexes_large_multibyte_value_once() -> None:
    glyph = "\U0001f1f8\U0001f1ea"
    original = "status: " + glyph * 40_000
    metrics = PrefixSearchMetrics()

    rendered = _truncate_rendered(original, 4096, _metrics=metrics)

    assert metrics.scanned_codepoints == len(original)
    assert metrics.safe_boundaries <= 40_009
    assert metrics.candidate_probes <= metrics.safe_boundaries.bit_length() + 1
    assert len(rendered.encode("utf-8")) <= 4096
    assert "rich status truncated" in rendered
    assert "\ufffd" not in rendered


def test_rich_status_limit_is_utf8_bytes_with_details() -> None:
    status = status_decoding.RichStatus(
        3,
        "invalid",
        (_decoded_detail(error_details_pb2.DebugInfo(detail="界" * 1_000)),),
    )

    rendered = render_rich_status(
        status, transport_code=3, max_rendered_chars=240
    )

    assert len(rendered.encode("utf-8")) <= 240
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in rendered
    assert "rich status truncated" in rendered
    assert "\ufffd" not in rendered


def test_large_standalone_rich_status_retains_status_reason_and_preview() -> (
    None
):
    status = status_decoding.RichStatus(
        3,
        "validation failed",
        (
            _decoded_detail(
                error_details_pb2.DebugInfo(detail="x" * (70 * 1024))
            ),
        ),
    )

    rendered = render_rich_status(status, transport_code=3)

    assert len(rendered) <= 64 * 1024
    assert "debug information:" in rendered
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in rendered
    assert "  reason: validation failed" in rendered
    assert "rich status truncated" in rendered


def test_standalone_rich_status_renders_each_detail_warning_once() -> None:
    detail = status_decoding.DecodedStatusDetail(
        "type.example/Unknown",
        "type.example.Unknown",
        b"opaque",
        warning="unknown message type",
    )
    status = status_decoding.RichStatus(
        3,
        "invalid",
        (detail,),
        warnings=("status payload had contextual mismatch",),
    )

    rendered = render_rich_status(status, transport_code=3)

    assert rendered.count("unknown message type") == 1
    assert rendered.count("status payload had contextual mismatch") == 1


def test_status_limits_and_stderr_writer() -> None:
    too_large = decode_status_details(b"12345", max_status_bytes=4)
    assert too_large is not None
    assert too_large.decoded is False
    assert "limit is 4" in too_large.warnings[0]

    decoded = decode_status_details(
        _status_bytes(("unknown.Type", b"abcdef")), max_detail_bytes=3
    )
    assert decoded is not None
    assert "limit is 3" in (decoded.details[0].warning or "")

    stream = io.StringIO()
    write_rich_status(
        decoded, stream=stream, transport_code=3, transport_message="bad"
    )
    assert "protodial: (E4103) INVALID_ARGUMENT (3)" in stream.getvalue()
    assert "  reason: bad" in stream.getvalue()


@pytest.mark.parametrize("limit", [1, 2, 17, 80, 256])
def test_rich_status_writer_includes_newline_in_utf8_byte_limit(
    limit: int,
) -> None:
    status = status_decoding.RichStatus(3, "界" * 200, ())
    stream = io.StringIO()

    write_rich_status(
        status,
        stream=stream,
        transport_code=3,
        max_rendered_chars=limit,
    )

    written = stream.getvalue()
    assert written.endswith("\n")
    assert len(written.encode("utf-8")) <= limit
    assert (written == "\n") is (limit == 1)


@pytest.mark.parametrize("limit", [0, -1])
def test_rich_status_writer_rejects_nonpositive_limits_without_writing(
    limit: int,
) -> None:
    stream = io.StringIO()

    with pytest.raises(ValueError, match="greater than zero"):
        write_rich_status(None, stream=stream, max_rendered_chars=limit)

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "name",
    [
        "max_status_bytes",
        "max_detail_bytes",
        "max_details",
        "max_recursion_depth",
    ],
)
@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_status_decode_limits_reject_invalid_values_before_input(
    name: str, value: object
) -> None:
    with pytest.raises(
        ValueError, match=rf"{name} must be a nonnegative integer"
    ):
        decode_status_details(None, **{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "max_status_bytes",
        "max_detail_bytes",
        "max_details",
        "max_recursion_depth",
    ],
)
@pytest.mark.parametrize("value", [0, 1])
def test_status_decode_limits_accept_nonnegative_integers(
    name: str, value: int
) -> None:
    assert decode_status_details(None, **{name: value}) is None
