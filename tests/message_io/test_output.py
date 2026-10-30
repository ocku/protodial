import io
import json
import struct
from types import SimpleNamespace
from typing import Any

from protodial.message_io import (
    message_to_json,
    render_response,
    write_response,
)

from .support import MESSAGE, POOL, _options


def test_json_version_adapter_emits_compact_canonical_json_with_defaults() -> (
    None
):
    rendered = message_to_json(
        MESSAGE(display_name="Ada"),
        POOL,
        _options(emit_defaults=True),
    )
    value = json.loads(rendered)
    assert value == {"displayName": "Ada", "count": 0, "data": ""}
    assert "\n" not in rendered


def test_response_rendering_for_all_formats() -> None:
    message = MESSAGE(display_name="Ada", count=7, data=b"raw")
    json_output = render_response(
        message, _options(output_format="json"), streaming=True, pool=POOL
    )
    assert json.loads(json_output) == {
        "displayName": "Ada",
        "count": 7,
        "data": "cmF3",
    }
    assert json_output.endswith(b"\n")

    text_output = render_response(
        message, _options(output_format="text"), streaming=True, pool=POOL
    )
    assert text_output.endswith(b"\n---\n")
    assert b'display_name: "Ada"' in text_output

    wire = message.SerializeToString()
    assert (
        render_response(
            message, _options(output_format="protobuf"), streaming=True
        )
        == struct.pack(">I", len(wire)) + wire
    )
    assert (
        render_response(
            message, _options(output_format="protobuf"), streaming=False
        )
        == wire
    )
    assert (
        render_response(
            message,
            _options(output_format="raw"),
            streaming=True,
            raw_field="data",
        )
        == b"raw"
    )


def test_write_response_flushes_binary_output_and_handles_partial_writes() -> (
    None
):
    class Output(io.BytesIO):
        flushed = False

        def write(self, data: Any) -> int:
            return super().write(bytes(data[:2]))

        def flush(self) -> None:
            self.flushed = True

    output = Output()
    write_response(
        MESSAGE(data=b"payload"),
        _options(output_format="raw"),
        SimpleNamespace(server_streaming=True),
        POOL,
        "data",
        output,
    )
    assert output.getvalue() == b"payload"
    assert output.flushed
