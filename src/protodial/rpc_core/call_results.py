"""Collect and present terminal RPC results."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, TypeVar

from ..commands.models import DiagnosticOptions
from ..request_metadata.types import Metadata
from .models import RpcSummary
from .protocols import RpcCall, RpcRuntime

if TYPE_CHECKING:
    from ..presentation.diagnostics.models import DiagnosticSink

_MessageT = TypeVar("_MessageT")
_RequestT = TypeVar("_RequestT")


def normalize_metadata(
    metadata: Iterable[tuple[object, str | bytes]] | None,
) -> Metadata:
    """Normalize grpcio metadata keys to strings."""
    if metadata is None:
        return []
    return [(str(key), value) for key, value in metadata]


def summarize_call(
    call: RpcCall[object],
    initial_metadata: Metadata,
    sent_messages: int,
    received_messages: int,
    started_at: float,
    grpc: RpcRuntime,
) -> RpcSummary:
    """Collect terminal metadata and status from a call."""
    trailing = call.trailing_metadata()
    unknown = grpc.StatusCode.UNKNOWN
    code = call.code()
    details = call.details()
    return RpcSummary(
        initial_metadata=initial_metadata,
        trailing_metadata=normalize_metadata(trailing),
        code=unknown if code is None else code,
        details=str(details),
        sent_messages=sent_messages,
        received_messages=received_messages,
        elapsed_seconds=time.monotonic() - started_at,
    )


def diagnostic_sink(diagnostics: DiagnosticOptions) -> DiagnosticSink:
    """Return the active sink or a standalone command-line sink."""
    from ..presentation.diagnostics.context import current_sink
    from ..presentation.diagnostics.rendering import StderrDiagnosticSink

    return current_sink() or StderrDiagnosticSink(
        diagnostics.verbosity,
        diagnostic_format=diagnostics.diagnostic_format,
        color=diagnostics.color,
    )


def write_initial_diagnostics(
    metadata: Metadata,
    diagnostics: DiagnosticOptions,
    *,
    sink: DiagnosticSink | None = None,
) -> None:
    """Emit response headers when they arrive from the server."""
    from ..presentation.diagnostics.models import DiagnosticEvent

    if diagnostics.show_headers:
        output = sink if sink is not None else diagnostic_sink(diagnostics)
        output.emit(
            DiagnosticEvent(
                "rpc.headers",
                level=0,
                metadata=tuple(metadata),
            )
        )


def write_terminal_diagnostics(
    summary: RpcSummary,
    diagnostics: DiagnosticOptions,
    *,
    include_non_ok_status: bool = False,
    sink: DiagnosticSink | None = None,
) -> None:
    """Emit response trailers and status after the body completes."""
    from ..presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
        DiagnosticValue,
    )

    output = sink if sink is not None else diagnostic_sink(diagnostics)
    if diagnostics.show_trailers:
        output.emit(
            DiagnosticEvent(
                "rpc.trailers",
                level=0,
                metadata=tuple(summary.trailing_metadata),
            )
        )
    code_name = getattr(summary.code, "name", str(summary.code))
    if diagnostics.show_status and (code_name == "OK" or include_non_ok_status):
        from ..presentation.rpc import status_parts

        _, code_number = status_parts(summary.code)
        fields: dict[str, DiagnosticValue] = {
            "status": code_name,
            "grpc_status": {
                "name": code_name,
                "number": code_number,
            },
            "elapsed_seconds": summary.elapsed_seconds,
            "sent": summary.sent_messages,
            "received": summary.received_messages,
            "sent_messages": summary.sent_messages,
            "received_messages": summary.received_messages,
        }
        output.emit(
            DiagnosticEvent(
                "rpc.completed",
                level=0,
                severity=(
                    DiagnosticSeverity.STATUS
                    if code_name == "OK"
                    else DiagnosticSeverity.WARNING
                ),
                code=None if code_name == "OK" else "W4101",
                fields=fields,
                causes=(
                    (summary.details,)
                    if code_name != "OK" and summary.details
                    else ()
                ),
            )
        )


def collect_call_summary(
    call: RpcCall[object],
    initial_metadata: Metadata,
    sent_messages: int,
    received_messages: int,
    started_at: float,
    grpc: RpcRuntime,
) -> RpcSummary:
    """Collect one terminal call result without presenting it."""
    if not initial_metadata:
        metadata = call.initial_metadata()
        initial_metadata = normalize_metadata(metadata)
    return summarize_call(
        call,
        initial_metadata,
        sent_messages,
        received_messages,
        started_at,
        grpc,
    )
