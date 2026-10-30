import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import protodial.cli as cli
import protodial.schema.context as schema_context
from protodial.commands.models import Command
from protodial.rpc import (
    open_input,
)
from protodial.schema.context import _export_after_use

from .support import FakeRpcError


@pytest.mark.parametrize(
    ("kind", "option", "option_value", "argument", "description"),
    [
        ("proto", "--proto", "{path}", "--proto", "proto source file"),
        ("protoset", "--protoset", "{path}", "--protoset", "protoset file"),
        ("data", "--data", "@{path}", "--data", "request data file"),
        (
            "field",
            "--field:file",
            "content={path}",
            "--field:file",
            "field file",
        ),
    ],
)
@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_local_input_errors_are_typed_and_exactly_located(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kind: str,
    option: str,
    option_value: str,
    argument: str,
    description: str,
    attached: bool,
    diagnostic_format: str,
) -> None:
    from protodial.schema.local import local_descriptor_source

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    path = str(tmp_path / f"missing-{kind}")
    value_text = option_value.format(path=path)
    supplied = [f"{option}={value_text}"] if attached else [option, value_text]
    if kind in {"proto", "protoset"}:
        arguments = [
            "list",
            "--diagnostic-format",
            diagnostic_format,
            *supplied,
            "unused.invalid",
        ]

        def fail(command: Command, _grpc: object) -> None:
            local_descriptor_source(command)

        monkeypatch.setattr(cli, "list_symbols", fail)
    else:
        arguments = [
            "--diagnostic-format",
            diagnostic_format,
            *supplied,
            "grpc://localhost:50051/example.Echo/Call",
        ]

        def fail(command: Command, _grpc: object) -> None:
            assert command.invoke_options is not None
            options = command.invoke_options
            with open_input(
                options.data,
                argument=options.data_argument,
                option_strings=options.data_option_strings,
                option_value=options.data_option_value,
            ):
                pass

        monkeypatch.setattr(cli, "invoke", fail)
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    token = supplied[0] if attached else value_text
    path_start = token.rfind(path)
    source = f"argument {arguments.index(token) + 1}"
    if diagnostic_format == "json":
        result = json.loads(captured.err)
        assert result["event"] == "local_io.failed"
        assert result["code"] == "E8002"
        assert result["message"] == f"could not read {description} `{path}`"
        assert result["context"]["operation"] == "read"
        assert result["context"]["path"] == path
        assert result["context"]["argument"] == argument
        assert result["causes"] == ["No such file or directory"]
        assert result["spans"] == [
            {
                "source": source,
                "text": token,
                "start": path_start,
                "end": path_start + len(path),
                "label": "path could not be read",
            }
        ]
    else:
        prose = " ".join(captured.err.split())
        assert f"protodial: (E8002) could not read {description}" in prose
        assert f"--> {source}" in captured.err
        assert Path(path).name in captured.err
        assert "No such file or directory" in prose
        assert "exists and is readable" in prose
    assert "/protodial-" not in captured.err


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("diagnostic_format", ["human", "json"])
def test_descriptor_export_error_is_typed_located_and_hides_temporary_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    attached: bool,
    diagnostic_format: str,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    path = str(tmp_path / "schema.pb")
    temporary = str(tmp_path / ".schema.pb.random")
    supplied = (
        [f"--protoset-out={path}"] if attached else ["--protoset-out", path]
    )
    arguments = [
        "--diagnostic-format",
        diagnostic_format,
        *supplied,
        "grpc://localhost:50051/example.Echo/Call",
    ]

    def export_failure(_source: object, _path: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", temporary)

    def fail(_command: Command, _grpc: object) -> None:
        with _export_after_use(SimpleNamespace(), path):
            pass

    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "invoke", fail)
    monkeypatch.setattr(schema_context, "write_descriptor_set", export_failure)

    assert cli.main(arguments) == 1
    captured = capsys.readouterr()
    token = supplied[0] if attached else path
    start = token.rfind(path)
    source = f"argument {arguments.index(token) + 1}"
    if diagnostic_format == "json":
        result = json.loads(captured.err)
        assert result["event"] == "descriptor.export_failed"
        assert result["code"] == "E8002"
        assert result["context"] == {
            "operation": "write",
            "path": path,
            "argument": "--protoset-out",
            "errno": errno.EACCES,
        }
        assert result["causes"] == ["Permission denied"]
        assert result["spans"] == [
            {
                "source": source,
                "text": token,
                "start": start,
                "end": start + len(path),
                "label": "export destination could not be written",
            }
        ]
        assert "writable" in result["help"][0]
    else:
        assert (
            "protodial: (E8002) could not export descriptor set" in captured.err
        )
        assert f"--> {source}" in captured.err
        assert Path(path).name in captured.err
        assert "Permission denied" in captured.err
        assert "writable" in captured.err
    assert temporary not in captured.err
