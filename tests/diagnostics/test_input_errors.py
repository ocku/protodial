from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from protodial import cli
from protodial.argument_parser import parse_arguments
from protodial.message_io import iter_json_values
from protodial.message_io.models import (
    JsonMessageSchemaError,
    MessageParseError,
)
from protodial.presentation.diagnostics.error_events import (
    ordinary_error_event,
)
from protodial.presentation.diagnostics.models import (
    DiagnosticSpan,
)
from protodial.presentation.diagnostics.rendering import (
    render_human,
    render_json,
)


def test_cli_malformed_literal_has_rust_style_source_span(
    monkeypatch, capsys
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise MessageParseError(
            "invalid JSON request 1 at line 2, column 3: expected value"
        )

    monkeypatch.setattr(cli, "invoke", fail)
    exit_code = cli.main(
        [
            "--color",
            "never",
            "--data",
            "{\n  bad\n}",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("protodial: (E2101) ")
    assert "--> --data:2:3" in captured.err
    assert "2 |   bad" in captured.err
    assert "^ invalid input" in captured.err


def test_cli_json_decoder_recursion_error_is_an_input_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    raw_decode = json.JSONDecoder.raw_decode

    def fail_decode(
        decoder: json.JSONDecoder, value: str, idx: int = 0
    ) -> tuple[object, int]:
        if value == "[":
            raise RecursionError("maximum recursion depth exceeded")
        return raw_decode(decoder, value, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", fail_decode)

    def invoke(*_args: object) -> None:
        list(
            iter_json_values(
                io.BytesIO(b"["),
                SimpleNamespace(max_input_buffer=16),
            )
        )

    monkeypatch.setattr(cli, "invoke", invoke)
    exit_code = cli.main(
        [
            "--diagnostic-format",
            "json",
            "--data",
            "[",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert exit_code == 1
    assert diagnostic["event"] == "input.invalid"
    assert diagnostic["code"] == "E2101"
    assert diagnostic["causes"] == [
        "invalid JSON request 1 at line 1, column 1: JSON nesting is too deep"
    ]


@pytest.mark.parametrize(
    ("data", "column"),
    [("{", 2), ('{"name":', 9), ("[1,", 4)],
)
def test_cli_malformed_literal_at_eof_has_bounded_insertion_span(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: str,
    column: int,
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise MessageParseError(
            f"invalid JSON request 1 at line 1, column {column}: expected value"
        )

    monkeypatch.setattr(cli, "invoke", fail)
    exit_code = cli.main(
        [
            "--color",
            "never",
            "--data",
            data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("protodial: (E2101) ")
    assert f"--> --data:1:{column}" in captured.err
    assert f"1 | {data}" in captured.err
    assert "^ invalid input" in captured.err


@pytest.mark.parametrize("source_kind", ["literal", "file"])
def test_textproto_parser_position_locates_multiline_input(
    tmp_path: Path, source_kind: str
) -> None:
    data = 'name: "Ada"\nbroken'
    if source_kind == "file":
        source_path = tmp_path / "request.textproto"
        source_path.write_text(data, encoding="utf-8")
        configured_data = f"@{source_path}"
        expected_source = str(source_path)
    else:
        configured_data = data
        expected_source = "--data"
    command = parse_arguments(
        [
            "--input-format",
            "text",
            "--data",
            configured_data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(
        MessageParseError(
            "invalid text request 1: 2:1 : Message type has no field named "
            '"broken".'
        ),
        command,
    )

    assert event.spans == (
        DiagnosticSpan(
            expected_source,
            line=2,
            column=1,
            text="broken",
            start=0,
            end=1,
            label="invalid input",
        ),
    )


def test_textproto_position_parser_ignores_unrelated_colons() -> None:
    data = 'name: "Ada"\nbroken'
    command = parse_arguments(
        [
            "--input-format",
            "text",
            "--data",
            data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(
        MessageParseError("invalid text request 1: could not parse value: 2:1"),
        command,
    )

    assert event.spans[0].line is None
    assert event.spans[0].column is None
    assert event.spans[0].text == 'name: "Ada"'


@pytest.mark.parametrize("input_format", ["text", "protobuf", "raw"])
def test_non_json_parse_errors_omit_json_only_remediation(
    input_format: str,
) -> None:
    command = parse_arguments(
        [
            "--input-format",
            input_format,
            "--data",
            "invalid",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(MessageParseError("invalid request"), command)
    machine = json.loads(render_json(event, 1))

    assert event.help == ()
    assert "validate JSON" not in render_human(event)
    assert "help" not in machine


def test_json_parse_error_retains_json_remediation() -> None:
    command = parse_arguments(
        [
            "--data",
            "invalid",
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    event = ordinary_error_event(MessageParseError("invalid request"), command)

    assert event.help == ("validate JSON input with `jq .`",)
    assert json.loads(render_json(event, 1))["help"] == [
        "validate JSON input with `jq .`"
    ]


def test_valid_json_schema_error_names_expected_protojson_message() -> None:
    command = parse_arguments(
        [
            "--data",
            '{"a": "b"}',
            "grpc://localhost:50051/example.Service/Call",
        ]
    )
    error = JsonMessageSchemaError(
        1,
        "example.Request",
        'Message type "example.Request" has no field named "a"',
    )

    event = ordinary_error_event(error, command)
    human = render_human(event)
    machine = json.loads(render_json(event, 1))

    assert event.message == (
        "request body is valid JSON but does not match the ProtoJSON schema "
        "for example.Request"
    )
    assert event.causes == (
        'request 1: Message type "example.Request" has no field named "a"',
    )
    assert event.help == (
        "match field names and values to the example.Request schema",
    )
    assert event.fields == {
        "kind": "protojson_schema",
        "request_type": "example.Request",
        "request_index": 1,
    }
    assert "validate JSON input with `jq .`" not in human
    assert machine["message"] == event.message
    assert machine["causes"] == list(event.causes)

    narrow_lines = render_human(event, width=40).splitlines()
    assert narrow_lines[0] == f"protodial: (E2101) {event.message}"
    reason_line = next(
        line for line in narrow_lines if line.startswith("  reason:")
    )
    assert reason_line == (f"  reason: {event.causes[0]}")


@pytest.mark.parametrize("data", ["-", "@-"])
def test_cli_malformed_stdin_forms_have_stdin_source_span(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: str,
) -> None:
    class FakeRpcError(Exception):
        pass

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(*_args: object) -> None:
        raise MessageParseError(
            "invalid JSON request 1 at line 2, column 3: expected value"
        )

    monkeypatch.setattr(cli, "invoke", fail)
    exit_code = cli.main(
        [
            "--color",
            "never",
            "--data",
            data,
            "grpc://localhost:50051/example.Service/Call",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "--> <stdin>:2:3" in captured.err
