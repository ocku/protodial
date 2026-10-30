from __future__ import annotations

from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
)

from protodial.rich_status import (
    decode_status_details,
    render_rich_status,
)
from protodial.rich_status import decoding as status_decoding

from .status_support import _custom_pool_and_payload, _status_bytes


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
