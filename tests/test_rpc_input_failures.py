from __future__ import annotations

from collections.abc import Iterator
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

import protodial.rpc as rpc_module
from protodial.commands.models import DiagnosticOptions, InvokeOptions
from protodial.errors import CliError
from protodial.rpc import invoke_rpc


class FakeStatus:
    def __init__(self, name: str) -> None:
        self.name = name


OK = FakeStatus("OK")
CANCELLED = FakeStatus("CANCELLED")
UNKNOWN = FakeStatus("UNKNOWN")


class FakeRpcError(Exception):
    pass


class AsyncRequestCall:
    def __init__(
        self, responses: list[bytes], *, finish_after_requests: bool = False
    ) -> None:
        self.responses = responses
        self.finish_after_requests = finish_after_requests
        self.cancelled = False
        self.producer_error: Exception | None = None
        self.producer_done = Event()

    def consume(self, requests: Iterator[bytes]) -> None:
        def run() -> None:
            try:
                list(requests)
            except Exception as error:
                self.producer_error = error
            finally:
                self.producer_done.set()

        Thread(target=run, daemon=True).start()

    def __iter__(self) -> Iterator[bytes]:
        yield from self.responses
        if self.finish_after_requests:
            assert self.producer_done.wait(1)

    def initial_metadata(self) -> list[tuple[str, str]]:
        return []

    def trailing_metadata(self) -> list[tuple[str, str]]:
        return []

    def code(self) -> FakeStatus:
        return CANCELLED if self.cancelled else OK

    def details(self) -> str:
        return "cancelled" if self.cancelled else ""

    def result(self) -> bytes:
        if self.finish_after_requests:
            assert self.producer_done.wait(1)
        return self.responses[0]

    def cancel(self) -> None:
        self.cancelled = True


class AsyncRequestRpc:
    def __init__(self, call: AsyncRequestCall) -> None:
        self.call = call

    def __call__(
        self, requests: Iterator[bytes], **_kwargs: Any
    ) -> AsyncRequestCall:
        self.call.consume(requests)
        return self.call

    def future(
        self, requests: Iterator[bytes], **_kwargs: Any
    ) -> AsyncRequestCall:
        self.call.consume(requests)
        return self.call


OPTIONS = InvokeOptions("{}", "json", "json", None, None, 1024)
DIAGNOSTICS = DiagnosticOptions(False, False, False)
FAKE_GRPC = SimpleNamespace(
    RpcError=FakeRpcError,
    StatusCode=SimpleNamespace(UNKNOWN=UNKNOWN),
)


def run_rpc(
    monkeypatch: pytest.MonkeyPatch,
    call: AsyncRequestCall,
    requests: Iterator[bytes],
    written: list[bytes],
    *,
    server_streaming: bool,
) -> Any:
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    method = SimpleNamespace(
        client_streaming=True, server_streaming=server_streaming
    )
    result = invoke_rpc(
        AsyncRequestRpc(call),
        requests,
        OPTIONS,
        1.0,
        method,
        [],
        None,
        None,
        FAKE_GRPC,
        DIAGNOSTICS,
    )
    return result


def test_stream_unary_input_error_aborts_call_and_suppresses_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = AsyncRequestCall([b"success"], finish_after_requests=True)
    written: list[bytes] = []

    def requests() -> Iterator[bytes]:
        yield b"accepted-before-error"
        raise CliError("invalid request input")

    with pytest.raises(CliError, match="invalid request input"):
        run_rpc(monkeypatch, call, requests(), written, server_streaming=False)

    assert call.producer_done.wait(1)
    assert isinstance(call.producer_error, CliError)
    assert call.cancelled is True
    assert written == []


def test_stream_stream_does_not_wait_for_open_request_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = AsyncRequestCall([b"response"])
    producer_waiting = Event()
    release_producer = Event()
    invocation_done = Event()
    outcome: dict[str, Any] = {}
    written: list[bytes] = []

    def requests() -> Iterator[bytes]:
        yield b"one"
        producer_waiting.set()
        assert release_producer.wait(2)
        yield b"two"

    def invoke() -> None:
        try:
            outcome["summary"] = run_rpc(
                monkeypatch, call, requests(), written, server_streaming=True
            )
        except BaseException as error:
            outcome["error"] = error
        finally:
            invocation_done.set()

    thread = Thread(target=invoke)
    thread.start()
    assert producer_waiting.wait(1)
    try:
        assert invocation_done.wait(1)
        assert "error" not in outcome
        assert outcome["summary"].sent_messages == 1
        assert written == [b"response"]
        assert call.cancelled is False
    finally:
        release_producer.set()
        thread.join(2)
    assert call.producer_done.wait(1)


def test_stream_stream_waits_for_request_error_and_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = AsyncRequestCall([b"partial-response"], finish_after_requests=True)
    producer_waiting = Event()
    release_producer = Event()
    invocation_done = Event()
    outcome: dict[str, Any] = {}
    written: list[bytes] = []

    def requests() -> Iterator[bytes]:
        yield b"one"
        producer_waiting.set()
        assert release_producer.wait(2)
        raise CliError("late request failure")

    def invoke() -> None:
        try:
            run_rpc(
                monkeypatch, call, requests(), written, server_streaming=True
            )
        except BaseException as error:
            outcome["error"] = error
        finally:
            invocation_done.set()

    thread = Thread(target=invoke)
    thread.start()
    assert producer_waiting.wait(1)
    release_producer.set()
    thread.join(2)

    assert invocation_done.is_set()
    assert isinstance(outcome.get("error"), CliError)
    assert str(outcome["error"]) == "late request failure"
    assert call.cancelled is True
    assert written == [b"partial-response"]
