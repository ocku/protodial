from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from protodial import cli
from protodial.presentation.diagnostics.context import use_sink
from protodial.presentation.diagnostics.rendering import (
    StderrDiagnosticSink,
    _display_width,
)
from protodial.schema.local import _emit_compiler_warnings


def test_invalid_proto_stderr_is_one_centralized_ndjson_event(
    tmp_path, capfd
) -> None:
    proto = tmp_path / "invalid.proto"
    proto.write_text("this is not protobuf\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--diagnostic-format",
            "json",
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert value["event"] == "command.failed"
    assert value["message"] == "proto compilation failed"
    assert any("compiler:" in note for note in value["notes"])


def test_invalid_proto_native_stderr_uses_human_diagnostic_grammar(
    tmp_path, capfd
) -> None:
    proto = tmp_path / "invalid-human.proto"
    proto.write_text("not a valid proto\n", encoding="utf-8")

    exit_code = cli.main(
        ["list", "--color", "never", "--proto", str(proto), "unused.invalid"]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith(
        "protodial: (E2001) proto compilation failed\n"
    )
    assert f"--> {proto}:1:1" in captured.err.replace("\n      ", "")
    assert "Expected top-level statement" in captured.err
    assert "  note: compiler:" in captured.err
    assert not any(
        line.startswith(("WARNING:", "W0000", str(proto)))
        for line in captured.err.splitlines()
    )


@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_proto_compiler_eof_location_is_a_bounded_insertion_span(
    tmp_path: Path, capfd, diagnostic_format: str
) -> None:
    proto = tmp_path / "unterminated.proto"
    proto.write_text('syntax = "proto3";\nmessage X {', encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--color",
            "never",
            "--diagnostic-format",
            diagnostic_format,
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Traceback" not in captured.err
    if diagnostic_format == "json":
        value = json.loads(captured.err)
        assert (value["code"], value["event"]) == ("E2001", "command.failed")
        assert value["spans"] == [
            {
                "source": str(proto),
                "line": 2,
                "column": 12,
                "text": "message X {",
                "start": 11,
                "end": 11,
                "label": "invalid schema input",
            }
        ]
    else:
        assert captured.err.startswith(
            "protodial: (E2001) proto compilation failed\n"
        )
        assert f"--> {proto}:2:12" in captured.err.replace("\n      ", "")
        assert "message X {" in captured.err


@pytest.mark.parametrize("character", ["å", "😀"])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_proto_compiler_byte_columns_select_character_after_unicode(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    character: str,
    diagnostic_format: str,
) -> None:
    proto = tmp_path / "unicode-location.proto"
    source_line = (
        f'message X {{ string ok = 1 [json_name = "{character}"] nope; }}'
    )
    proto.write_text(f'syntax = "proto3";\n{source_line}\n', encoding="utf-8")

    exit_code = cli.main(
        [
            "list",
            "--color",
            "never",
            "--diagnostic-format",
            diagnostic_format,
            "--proto",
            str(proto),
            "unused.invalid",
        ]
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    intended = source_line.index("nope")
    if diagnostic_format == "json":
        value = json.loads(captured.err)
        span = value["spans"][0]
        assert span["text"][span["start"] : span["end"]] == "n"
        assert (span["start"], span["end"]) == (intended, intended + 1)
    else:
        lines = captured.err.splitlines()
        source_index = lines.index(f" 2 | {source_line}")
        marker = lines[source_index + 1]
        marker_indent = _display_width(source_line[:intended])
        assert marker == (f"   | {' ' * marker_indent}^ invalid schema input")


@pytest.mark.parametrize("character", ["å", "😀"])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_proto_warning_byte_columns_select_character_after_unicode(
    tmp_path: Path, character: str, diagnostic_format: str
) -> None:
    proto = tmp_path / "warning-location.proto"
    source_line = f'option java_package = "{character}"; unused'
    proto.write_text(source_line + "\n", encoding="utf-8")
    intended = source_line.index("unused")
    byte_column = len(source_line[:intended].encode("utf-8")) + 1
    stream = io.StringIO()
    sink = StderrDiagnosticSink(
        stream=stream,
        diagnostic_format=diagnostic_format,
        color="never",
    )

    with use_sink(sink):
        _emit_compiler_warnings(
            (f"{proto}:1:{byte_column}: synthetic compiler warning",),
            (tmp_path,),
        )

    rendered = stream.getvalue()
    if diagnostic_format == "json":
        value = json.loads(rendered)
        span = value["spans"][0]
        assert span["text"][span["start"] : span["end"]] == "u"
        assert (span["start"], span["end"]) == (intended, intended + 1)
    else:
        lines = rendered.splitlines()
        source_index = lines.index(f" 1 | {source_line}")
        marker = lines[source_index + 1]
        marker_indent = _display_width(source_line[:intended])
        assert marker == f"   | {' ' * marker_indent}^ compiler warning"
