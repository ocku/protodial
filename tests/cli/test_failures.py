import io
from types import SimpleNamespace

import pytest

import protodial.cli as cli
import protodial.schema.context as schema_context
from protodial.commands.models import Command
from protodial.errors import CliError
from protodial.formats import MessageFormat
from protodial.message_io import iter_requests
from protodial.rpc import (
    RpcFailure,
    RpcSummary,
)
from protodial.schema.context import _export_after_use

from .support import FakeRpcError


def test_main_writes_domain_errors_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(_command: Command, _grpc: object) -> None:
        raise CliError("schema unavailable")

    monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "protodial: (E2001) schema unavailable\n"


def test_main_escapes_controls_in_reflection_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(_command: Command, _grpc: object) -> None:
        raise CliError("reflection: bad\nforged\rline\x1b[2J\u202eowned")

    monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "protodial: (E2001) reflection: "
        "bad\\nforged\\rline\\x1b[2J\\u202eowned\n"
    )
    assert "\x1b" not in captured.err


@pytest.mark.parametrize("input_format", list(MessageFormat))
@pytest.mark.parametrize(
    ("client_streaming", "expected_count"), [(False, 1), (True, 0)]
)
def test_main_resolves_omitted_input_after_method_cardinality_is_known(
    monkeypatch: pytest.MonkeyPatch,
    input_format: MessageFormat,
    client_streaming: bool,
    expected_count: int,
) -> None:
    from google.protobuf import descriptor_pool, empty_pb2

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def inspect_input(command: Command, _grpc: object) -> None:
        options = command.invoke_options
        assert options is not None
        requests = iter_requests(
            io.BytesIO(),
            options,
            SimpleNamespace(
                client_streaming=client_streaming, server_streaming=False
            ),
            empty_pb2.Empty,
            descriptor_pool.Default(),
            "value" if input_format is MessageFormat.RAW else None,
        )
        assert list(requests) == [empty_pb2.Empty()] * expected_count

    monkeypatch.setattr(cli, "invoke", inspect_input)

    assert (
        cli.main(
            [
                "--input-format",
                input_format.value,
                "grpc://localhost:50051/example.Echo/Call",
            ]
        )
        == 0
    )


@pytest.mark.parametrize("primary_kind", ["rpc", "cli"])
def test_main_reports_secondary_export_failure_without_replacing_primary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    primary_kind: str,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def export_failure(_source: object, _path: str) -> None:
        error = OSError("disk full\nforged")
        setattr(
            error,
            "_protodial_notes",
            ("descriptor temporary-file cleanup also failed: unlink",),
        )
        raise error

    monkeypatch.setattr(schema_context, "write_descriptor_set", export_failure)
    if primary_kind == "rpc":
        summary = RpcSummary(
            [], [], SimpleNamespace(name="INTERNAL"), "primary", 0, 0, 0.1
        )
        primary: BaseException = RpcFailure(
            FakeRpcError("primary"), summary, False
        )
        argv = ["grpc://localhost:50051/example.Echo/Call"]

        def fail(_command: Command, _grpc: object) -> None:
            with _export_after_use(SimpleNamespace(), "output.pb"):
                raise primary

        monkeypatch.setattr(cli, "invoke", fail)
    else:
        primary = CliError("primary")
        argv = ["describe", "localhost:50051", "example.Echo"]

        def fail(_command: Command, _grpc: object) -> None:
            with _export_after_use(SimpleNamespace(), "output.pb"):
                raise primary

        monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert "primary" in captured.err
    assert "note: descriptor export also failed: disk full\\nforged" in (
        captured.err
    )
    assert "descriptor temporary-file cleanup also failed:" in captured.err
    assert "unlink" in captured.err
    assert captured.err.index("primary") < captured.err.index(
        "descriptor export also failed"
    )
    assert getattr(primary, "_protodial_notes") == (
        "descriptor export also failed: disk full\nforged",
        "descriptor export also failed detail: descriptor temporary-file "
        "cleanup also failed: unlink",
    )


def test_main_reports_unexpected_type_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def fail(_command: Command, _grpc: object) -> None:
        raise TypeError("internal defect")

    monkeypatch.setattr(cli, "describe", fail)

    assert cli.main(["describe", "localhost:50051", "example.Echo"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("protodial: (E9001) internal defect")
    assert "Traceback" not in captured.err


def test_main_does_not_repeat_status_already_rendered_by_invocation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    summary = RpcSummary(
        [], [], SimpleNamespace(name="INTERNAL"), "failed", 1, 0, 0.1
    )

    def fail(_command: Command, _grpc: object) -> None:
        raise RpcFailure(FakeRpcError("failed"), summary, status_rendered=True)

    monkeypatch.setattr(cli, "invoke", fail)

    assert (
        cli.main(["--show-status", "grpc://localhost:50051/example.Echo/Call"])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("INTERNAL") == 1
    assert "gRPC INTERNAL" not in captured.err


def test_main_maps_keyboard_interrupt_to_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def interrupt(_command: Command, _grpc: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "invoke", interrupt)

    assert cli.main(["grpc://localhost:50051/example.Echo/Call"]) == 130


def test_main_keeps_interrupted_status_single_and_non_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from protodial.presentation.diagnostics.context import current_sink
    from protodial.presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
    )

    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)

    def interrupt(_command: Command, _grpc: object) -> None:
        sink = current_sink()
        assert sink is not None
        sink.emit(
            DiagnosticEvent(
                "rpc.completed",
                level=0,
                severity=DiagnosticSeverity.WARNING,
                code="W4101",
                fields={
                    "status": "CANCELLED",
                    "sent": 1,
                    "received": 0,
                },
            )
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "invoke", interrupt)

    assert (
        cli.main(
            [
                "--show-status",
                "grpc://localhost:50051/example.Echo/Call",
            ]
        )
        == 130
    )
    captured = capsys.readouterr()
    assert captured.err.count("CANCELLED") == 1
    assert captured.err == "* CANCELLED; sent 1 message; received 0 messages\n"
    assert "error" not in captured.err
