from types import SimpleNamespace
from typing import Any

import pytest

import protodial.rpc as rpc_module
from protodial.commands.models import DiagnosticOptions, InvokeOptions
from protodial.rpc import RpcSummary, invoke_rpc


class FakeStatus:
    def __init__(self, name: str) -> None:
        self.name = name


OK = FakeStatus("OK")


UNKNOWN = FakeStatus("UNKNOWN")


INTERNAL = FakeStatus("INTERNAL")


CANCELLED = FakeStatus("CANCELLED")


class FakeRpcError(Exception):
    def __init__(
        self, code: FakeStatus = INTERNAL, details: str = "failed"
    ) -> None:
        super().__init__(details)
        self._code = code
        self._details = details

    def code(self) -> FakeStatus:
        return self._code

    def details(self) -> str:
        return self._details


class FakeCall:
    def __init__(
        self,
        responses: list[Any],
        *,
        code: FakeStatus = OK,
        details: str = "",
        failure: BaseException | None = None,
    ) -> None:
        self.responses = responses
        self._code = code
        self._details = details
        self.failure = failure
        self.cancelled = False

    def __iter__(self):
        yield from self.responses
        if self.failure is not None:
            raise self.failure

    def initial_metadata(self) -> list[tuple[str, str | bytes]]:
        return [
            ("header", "one"),
            ("header", "two"),
            ("header-bin", b"\x00\xff"),
        ]

    def trailing_metadata(self) -> list[tuple[str, str | bytes]]:
        return [("trailer", "done"), ("trailer", "again")]

    def code(self) -> FakeStatus:
        return self._code

    def details(self) -> str:
        return self._details

    def result(self) -> Any:
        if self.failure is not None:
            raise self.failure
        return self.responses[0]

    def cancel(self) -> None:
        self.cancelled = True
        self._code = CANCELLED


class FakeRpc:
    def __init__(
        self, call: FakeCall, client_streaming: bool, server_streaming: bool
    ) -> None:
        self.call = call
        self.client_streaming = client_streaming
        self.server_streaming = server_streaming
        self.call_options: dict[str, Any] = {}

    def _consume_requests(self, request: Any) -> None:
        if self.client_streaming:
            list(request)

    def __call__(self, request: Any, **kwargs: Any) -> FakeCall:
        assert self.server_streaming
        self.call_options = kwargs
        self._consume_requests(request)
        return self.call

    def future(self, request: Any, **kwargs: Any) -> FakeCall:
        assert not self.server_streaming
        self.call_options = kwargs
        self._consume_requests(request)
        return self.call


FAKE_GRPC = SimpleNamespace(
    RpcError=FakeRpcError, StatusCode=SimpleNamespace(UNKNOWN=UNKNOWN)
)


NO_DIAGNOSTICS = DiagnosticOptions(False, False, False)


ALL_DIAGNOSTICS = DiagnosticOptions(True, True, True)


TEST_OPTIONS = InvokeOptions("{}", "json", "json", None, None, 1024)


def run_rpc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_streaming: bool,
    server_streaming: bool,
    call: FakeCall | None = None,
    diagnostics: DiagnosticOptions = NO_DIAGNOSTICS,
) -> tuple[RpcSummary, FakeCall, list[Any]]:
    responses = (
        ["response-1", "response-2"] if server_streaming else ["response"]
    )
    call = call or FakeCall(responses)
    written: list[Any] = []
    monkeypatch.setattr(
        rpc_module,
        "write_response",
        lambda message, *_args: written.append(message),
    )
    method = SimpleNamespace(
        client_streaming=client_streaming,
        server_streaming=server_streaming,
    )
    requests = iter(
        ["request-1", "request-2"] if client_streaming else ["request"]
    )

    summary = invoke_rpc(
        FakeRpc(call, client_streaming, server_streaming),
        requests,
        TEST_OPTIONS,
        1.0,
        method,
        [],
        None,
        None,
        FAKE_GRPC,
        diagnostics,
    )
    return summary, call, written
