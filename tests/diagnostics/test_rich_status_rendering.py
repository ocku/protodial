from __future__ import annotations

import pytest
from google.protobuf import (
    descriptor_pb2,
    duration_pb2,
    json_format,
)
from google.rpc import error_details_pb2

from protodial.rich_status import (
    decode_status_details,
    render_rich_status,
)
from protodial.rich_status import decoding as status_decoding

from .status_support import _decoded_detail


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
