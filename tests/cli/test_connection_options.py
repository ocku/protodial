import io
import sys
from types import SimpleNamespace

import pytest

from protodial.argument_parser import parse_arguments
from protodial.rpc import (
    open_input,
)


def test_curl_style_short_options() -> None:
    command = parse_arguments(
        [
            "grpcs://localhost:50051/example.Echo/Call",
            "-k",
            "-i",
            "-m",
            "2.5",
            "-A",
            "test-client/1.0",
        ]
    )

    assert command.transport.mode == "insecure"
    assert command.diagnostics.show_headers is True
    assert command.timeout == 2.5
    assert command.connection.user_agent == "test-client/1.0"


def test_wait_for_ready_is_preserved_for_invocation() -> None:
    command = parse_arguments(
        ["--wait-for-ready", "grpc://localhost:50051/example.Echo/Call"]
    )

    assert command.invoke_options is not None
    assert command.invoke_options.wait_for_ready is True


def test_wait_for_ready_help_explains_local_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--help"])

    assert error.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert (
        "--wait-for-ready let the RPC wait for readiness until --max-time; "
        "local schemas skip the connection preflight"
    ) in help_text


def test_connection_options_are_preserved_in_command_configuration() -> None:
    command = parse_arguments(
        [
            "--connect-timeout",
            "1.5",
            "--max-recv-message-size",
            "1024",
            "--max-send-message-size",
            "2048",
            "--compression",
            "gzip",
            "--keepalive-time",
            "2.25",
            "--authority",
            "route.example",
            "--servername",
            "certificate.example",
            "grpcs://localhost:50051/example.Echo/Call",
        ]
    )

    assert command.connection.connect_timeout == 1.5
    assert command.connection.max_recv_message_size == 1024
    assert command.connection.max_send_message_size == 2048
    assert command.connection.compression == "gzip"
    assert command.connection.keepalive_time == 2.25
    assert command.transport.authority == "route.example"
    assert command.transport.server_name == "certificate.example"


@pytest.mark.parametrize("data", ["-", "@-"])
def test_data_dash_forms_read_stdin(
    monkeypatch: pytest.MonkeyPatch, data: str
) -> None:
    stream = io.BytesIO(b'{"message":"hello"}')
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))

    with open_input(data) as source:
        assert source is stream
        assert source.read() == b'{"message":"hello"}'


def test_invocation_help_documents_both_stdin_forms(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "- or @- reads stdin" in help_text
    assert "read a file (- for stdin)" in help_text


def test_field_file_dash_selects_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.BytesIO(b"field contents")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
    command = parse_arguments(
        [
            "--field:file",
            "content=-",
            "grpcs://localhost:50051/example.Upload/Send",
        ]
    )

    assert command.invoke_options is not None
    assert command.invoke_options.data == "@-"
    assert command.invoke_options.input_field == "content"
    with open_input(command.invoke_options.data) as source:
        assert source is stream
        assert source.read() == b"field contents"
