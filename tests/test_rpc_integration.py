from __future__ import annotations

import io
import time
from concurrent import futures
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from google.protobuf import descriptor_pool, wrappers_pb2

import protodial.rpc as rpc_module
from protodial.commands.models import DiagnosticOptions, InvokeOptions
from protodial.formats import MessageFormat
from protodial.message_io import iter_requests
from protodial.rpc import RpcFailure, invoke_rpc

OPTIONS = InvokeOptions("{}", "json", "json", None, None, 1024)
DIAGNOSTICS = DiagnosticOptions(True, True, True)
HEADERS = (("header", "one"), ("header", "two"), ("header-bin", b"\x00\xff"))
TRAILERS = (("trailer", "done"), ("trailer", "again"))


def _identity(value: bytes) -> bytes:
    return value


def _start_response(context: grpc.ServicerContext) -> None:
    context.send_initial_metadata(HEADERS)
    context.set_trailing_metadata(TRAILERS)


def _unary_unary(request: bytes, context: grpc.ServicerContext) -> bytes:
    _start_response(context)
    return request.upper()


def _unary_stream(request: bytes, context: grpc.ServicerContext):
    _start_response(context)
    yield request + b"-1"
    yield request + b"-2"


def _stream_unary(requests: Any, context: grpc.ServicerContext) -> bytes:
    _start_response(context)
    return b"|".join(requests)


def _stream_stream(requests: Any, context: grpc.ServicerContext):
    _start_response(context)
    for request in requests:
        yield request.upper()


def _partial_failure(request: bytes, context: grpc.ServicerContext):
    _start_response(context)
    yield request + b"-1"
    yield request + b"-2"
    context.abort(grpc.StatusCode.INTERNAL, "stream failed")


def _early_stream_unary(_requests: Any, context: grpc.ServicerContext) -> bytes:
    _start_response(context)
    return b"early"


def _early_stream_stream(_requests: Any, context: grpc.ServicerContext):
    _start_response(context)
    yield b"early"


def _early_stream_failure(
    _requests: Any, context: grpc.ServicerContext
) -> bytes:
    context.abort(grpc.StatusCode.INTERNAL, "rejected early")


def _deadline_stream_unary(
    _requests: Any, context: grpc.ServicerContext
) -> bytes:
    while context.is_active():
        time.sleep(0.01)
    return b"too late"


@pytest.fixture
def channel():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    handlers = {
        "UnaryUnary": grpc.unary_unary_rpc_method_handler(_unary_unary),
        "UnaryStream": grpc.unary_stream_rpc_method_handler(_unary_stream),
        "StreamUnary": grpc.stream_unary_rpc_method_handler(_stream_unary),
        "StreamStream": grpc.stream_stream_rpc_method_handler(_stream_stream),
        "PartialFailure": grpc.unary_stream_rpc_method_handler(
            _partial_failure
        ),
        "EarlyStreamUnary": grpc.stream_unary_rpc_method_handler(
            _early_stream_unary
        ),
        "EarlyStreamStream": grpc.stream_stream_rpc_method_handler(
            _early_stream_stream
        ),
        "EarlyStreamFailure": grpc.stream_unary_rpc_method_handler(
            _early_stream_failure
        ),
        "DeadlineStreamUnary": grpc.stream_unary_rpc_method_handler(
            _deadline_stream_unary
        ),
    }
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("test.Results", handlers),)
    )
    port = server.add_insecure_port("localhost:0")
    server.start()
    client = grpc.insecure_channel(f"localhost:{port}")
    try:
        yield client
    finally:
        client.close()
        server.stop(None).wait()


@pytest.mark.parametrize(
    (
        "rpc_factory",
        "method_name",
        "client_streaming",
        "server_streaming",
        "sent",
        "received",
    ),
    [
        ("unary_unary", "UnaryUnary", False, False, 1, 1),
        ("unary_stream", "UnaryStream", False, True, 1, 2),
        ("stream_unary", "StreamUnary", True, False, 2, 1),
        ("stream_stream", "StreamStream", True, True, 2, 2),
    ],
)
def test_real_grpc_calls_produce_complete_summaries(
    monkeypatch: pytest.MonkeyPatch,
    channel: grpc.Channel,
    rpc_factory: str,
    method_name: str,
    client_streaming: bool,
    server_streaming: bool,
    sent: int,
    received: int,
) -> None:
    written: list[bytes] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    rpc = getattr(channel, rpc_factory)(
        f"/test.Results/{method_name}",
        request_serializer=_identity,
        response_deserializer=_identity,
    )
    method = SimpleNamespace(
        client_streaming=client_streaming,
        server_streaming=server_streaming,
    )
    requests = iter((b"one", b"two") if client_streaming else (b"one",))

    summary = invoke_rpc(
        rpc,
        requests,
        OPTIONS,
        2.0,
        method,
        [],
        None,
        None,
        grpc,
        DiagnosticOptions(False, False, False),
    )

    assert summary.initial_metadata == list(HEADERS)
    assert summary.trailing_metadata == list(TRAILERS)
    assert summary.code is grpc.StatusCode.OK
    assert summary.sent_messages == sent
    assert summary.received_messages == received
    assert len(written) == received


def test_real_partial_stream_preserves_payloads_and_final_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    channel: grpc.Channel,
) -> None:
    written: list[bytes] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    rpc = channel.unary_stream(
        "/test.Results/PartialFailure",
        request_serializer=_identity,
        response_deserializer=_identity,
    )
    method = SimpleNamespace(client_streaming=False, server_streaming=True)

    with pytest.raises(RpcFailure) as raised:
        invoke_rpc(
            rpc,
            iter((b"one",)),
            OPTIONS,
            2.0,
            method,
            [],
            None,
            None,
            grpc,
            DIAGNOSTICS,
        )

    assert written == [b"one-1", b"one-2"]
    assert raised.value.summary.code is grpc.StatusCode.INTERNAL
    assert raised.value.summary.details == "stream failed"
    assert raised.value.summary.trailing_metadata == list(TRAILERS)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "header-bin: base64:AP8=" in captured.err
    assert "< trailer: done\n< trailer: again\n<" in captured.err
    assert "status:" not in captured.err


class OpenRequests:
    """An iterator that models stdin remaining open after its first message."""

    def __init__(self) -> None:
        self.waiting = Event()
        self.release = Event()
        self._index = 0

    def __iter__(self) -> OpenRequests:
        return self

    def __next__(self) -> bytes:
        if self._index == 0:
            self._index += 1
            return b"one"
        if self._index == 1:
            self.waiting.set()
            if not self.release.wait(5):
                raise RuntimeError("test request producer was not released")
            self._index += 1
            return b"two"
        raise StopIteration


@pytest.mark.parametrize(
    ("method_name", "server_streaming", "timeout", "expected_code"),
    [
        ("EarlyStreamUnary", False, 2.0, None),
        ("EarlyStreamStream", True, 2.0, None),
        ("EarlyStreamFailure", False, 2.0, grpc.StatusCode.INTERNAL),
        ("DeadlineStreamUnary", False, 0.1, grpc.StatusCode.DEADLINE_EXCEEDED),
    ],
)
def test_terminal_client_stream_does_not_wait_for_open_request_producer(
    monkeypatch: pytest.MonkeyPatch,
    channel: grpc.Channel,
    method_name: str,
    server_streaming: bool,
    timeout: float,
    expected_code: grpc.StatusCode | None,
) -> None:
    written: list[bytes] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    rpc_factory = (
        channel.stream_stream if server_streaming else channel.stream_unary
    )
    rpc = rpc_factory(
        f"/test.Results/{method_name}",
        request_serializer=_identity,
        response_deserializer=_identity,
    )
    method = SimpleNamespace(
        client_streaming=True, server_streaming=server_streaming
    )
    requests = OpenRequests()
    outcome: dict[str, Any] = {}
    finished = Event()

    def run() -> None:
        try:
            outcome["summary"] = invoke_rpc(
                rpc,
                requests,
                OPTIONS,
                timeout,
                method,
                [],
                None,
                None,
                grpc,
                DiagnosticOptions(False, False, False),
            )
        except BaseException as error:
            outcome["error"] = error
        finally:
            finished.set()

    thread = Thread(target=run)
    thread.start()
    assert requests.waiting.wait(1)
    try:
        assert finished.wait(1), (
            "RPC remained blocked on its terminal request stream"
        )
        if expected_code is None:
            assert "error" not in outcome
            assert outcome["summary"].code is grpc.StatusCode.OK
            assert written == [b"early"]
        else:
            error = outcome.get("error")
            assert isinstance(error, RpcFailure)
            assert error.summary.code is expected_code
    finally:
        requests.release.set()
        thread.join(2)


@pytest.mark.parametrize(
    "input_format",
    [
        MessageFormat.JSON,
        MessageFormat.TEXT,
        MessageFormat.PROTOBUF,
        MessageFormat.RAW,
    ],
)
def test_real_client_stream_accepts_zero_messages_in_every_format(
    monkeypatch: pytest.MonkeyPatch,
    channel: grpc.Channel,
    input_format: MessageFormat,
) -> None:
    written: list[bytes] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    options = replace(OPTIONS, input_format=input_format)
    method = SimpleNamespace(client_streaming=True, server_streaming=False)
    requests = iter_requests(
        io.BytesIO(),
        options,
        method,
        wrappers_pb2.BytesValue,
        descriptor_pool.Default(),
        "value" if input_format is MessageFormat.RAW else None,
    )
    rpc = channel.stream_unary(
        "/test.Results/StreamUnary",
        request_serializer=wrappers_pb2.BytesValue.SerializeToString,
        response_deserializer=_identity,
    )

    summary = invoke_rpc(
        rpc,
        requests,
        options,
        2.0,
        method,
        [],
        descriptor_pool.Default(),
        None,
        grpc,
        DiagnosticOptions(False, False, False),
    )

    assert summary.sent_messages == 0
    assert summary.code is grpc.StatusCode.OK
    assert written == [b""]
