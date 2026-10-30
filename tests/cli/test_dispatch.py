import contextlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import protodial.cli as cli
import protodial.schema.context as schema_context
import protodial.transport as transport
from protodial.argument_parser import parse_arguments
from protodial.schema.paths import (
    default_proto_paths,
)

from .support import FakeRpcError


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (["--version"], 0),
        (["localhost:50051"], 2),
        (["list", "--proto", "does-not-exist.proto", "localhost:50051"], 1),
    ],
)
def test_module_exit_codes(arguments: list[str], exit_code: int) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "protodial", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == exit_code


def test_main_dispatches_discovery_payload_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        grpc=SimpleNamespace(RpcError=FakeRpcError), protobuf_errors=()
    )
    monkeypatch.setattr(cli, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        cli, "list_symbols", lambda _command, _grpc: print("example.Echo")
    )

    assert cli.main(["list", "localhost:50051"]) == 0
    assert all(Path(path).is_dir() for path in default_proto_paths())
    captured = capsys.readouterr()
    assert captured.out == "example.Echo\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("arguments", "require_channel"),
    [
        (
            [
                "--proto",
                "local.proto",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            True,
        ),
        (["list", "localhost:50051"], False),
    ],
)
def test_connection_events_precede_invocation_and_reflection_schema(
    arguments: list[str],
    require_channel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protodial.presentation.diagnostics.context import use_sink
    from protodial.presentation.diagnostics.models import (
        RecordingDiagnosticSink,
    )

    @contextlib.contextmanager
    def ready_channel(*_args: object, **_kwargs: object):
        yield object()

    source = object()
    monkeypatch.setattr(transport, "ready_channel", ready_channel)
    monkeypatch.setattr(
        schema_context,
        "build_descriptor_source",
        lambda *_args, **_kwargs: source,
    )
    command = parse_arguments(arguments)
    sink = RecordingDiagnosticSink()

    with use_sink(sink):
        with schema_context.open_descriptor_session(
            command,
            SimpleNamespace(),
            require_channel=require_channel,
        ) as session:
            assert session.source is source

    names = [event.name for event in sink.events]
    assert names == [
        "transport.connecting",
        "transport.connected",
        "schema.source",
    ]


@pytest.mark.parametrize(
    ("arguments", "expected_await_ready", "expected_connected"),
    [
        (
            [
                "--proto",
                "local.proto",
                "--wait-for-ready",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            False,
            False,
        ),
        (
            [
                "--proto",
                "local.proto",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            True,
            True,
        ),
        (
            [
                "--proto",
                "local.proto",
                "--use-reflection",
                "--wait-for-ready",
                "grpc://localhost:50051/example.Echo/Call",
            ],
            True,
            True,
        ),
    ],
)
def test_wait_for_ready_skips_preflight_only_for_local_invocation(
    arguments: list[str],
    expected_await_ready: bool,
    expected_connected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protodial.presentation.diagnostics.context import use_sink
    from protodial.presentation.diagnostics.models import (
        RecordingDiagnosticSink,
    )

    observed: list[bool] = []

    @contextlib.contextmanager
    def ready_channel(
        *_args: object, await_ready: bool = True
    ) -> Iterator[object]:
        observed.append(await_ready)
        yield object()

    monkeypatch.setattr(transport, "ready_channel", ready_channel)
    monkeypatch.setattr(
        schema_context,
        "build_descriptor_source",
        lambda *_args, **_kwargs: object(),
    )

    sink = RecordingDiagnosticSink()
    with use_sink(sink):
        with schema_context.open_descriptor_session(
            parse_arguments(arguments), SimpleNamespace(), require_channel=True
        ):
            pass

    assert observed == [expected_await_ready]
    names = [event.name for event in sink.events]
    assert ("transport.connected" in names) is expected_connected
