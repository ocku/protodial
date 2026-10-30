"""Semantic headlines for known diagnostic events."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ._text import (
    count,
    counter,
    duration_suffix,
    safe,
    status_parts,
)
from .models import (
    DiagnosticEvent,
    DiagnosticValue,
)


def _message_or(event: DiagnosticEvent, generated: str, sanitize: bool) -> str:
    """Prefer explicit prose when the generated headline lacks its subject."""
    if event.message:
        return safe(event.message) if sanitize else event.message
    return generated


def _message_count(fields: Mapping[str, DiagnosticValue], name: str) -> int:
    """Read an integer message counter with a safe default."""
    value = fields.get(name, 0)
    return (
        value if isinstance(value, int) and not isinstance(value, bool) else 0
    )


def _connecting_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render a connection-attempt headline."""
    target = safe(event.fields.get("target", ""))
    transport = safe(event.fields.get("transport", ""))
    suffix = f" using {transport} transport" if transport else ""
    return f"connecting to `{target}`{suffix}"


def _connected_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render a successful connection headline."""
    target = safe(event.fields.get("target", ""))
    return f"connected to `{target}`"


def _schema_source_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render the selected schema source."""
    source = safe(event.fields.get("source", "schema"))
    return f"using {source} schema"


def _reflection_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render the negotiated reflection protocol."""
    version = safe(event.fields.get("version", ""))
    return f"server reflection negotiated {version}"


def _resolved_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render a resolved RPC method and its type signature."""
    method = safe(event.fields.get("method", ""))
    if not method:
        return _message_or(event, "resolved RPC method", sanitize)
    cardinality = safe(event.fields.get("cardinality", ""))
    request_type = safe(event.fields.get("request_type", ""))
    response_type = safe(event.fields.get("response_type", ""))
    signature = (
        f"; `{request_type}` -> `{response_type}`"
        if request_type and response_type
        else ""
    )
    shape = f" ({cardinality}{signature})" if cardinality else ""
    return f"resolved `{method}`{shape}"


def _started_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render an RPC-start headline and compression mode."""
    method = safe(event.fields.get("method", ""))
    if not method:
        return _message_or(event, "calling RPC method", sanitize)
    compression = safe(event.fields.get("compression", ""))
    if compression == "none":
        suffix = " without compression"
    elif compression:
        suffix = f" using {compression} compression"
    else:
        suffix = ""
    return f"calling `{method}`{suffix}"


def _lifecycle_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render internal RPC lifecycle counters."""
    sent = _message_count(event.fields, "sent")
    received = _message_count(event.fields, "received")
    return (
        f"RPC lifecycle completed; sent {count(sent)}; "
        f"received {count(received)}"
    )


def _completed_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Render final status, timing, and message counters."""
    status_name, number = status_parts(event)
    status = safe(status_name) if sanitize else status_name
    if status != "OK" and number is not None:
        status = f"{status} ({number})"
    timing = duration_suffix(event.fields.get("elapsed_seconds"))
    sent = counter(event.fields, "sent", "sent_messages") or 0
    received = counter(event.fields, "received", "received_messages") or 0
    return f"{status}{timing}; sent {count(sent)}; received {count(received)}"


def _failed_message(event: DiagnosticEvent, sanitize: bool) -> str:
    """Build stable machine prose even when a failure supplied no message."""
    if event.message:
        return safe(event.message) if sanitize else event.message
    method_value = event.fields.get("method")
    method = str(method_value) if isinstance(method_value, str) else ""
    status_name, number = status_parts(event)
    if sanitize:
        method = safe(method)
        status_name = safe(status_name)
    status = f"{status_name} ({number})" if number is not None else status_name
    subject = f"RPC `{method}`" if method else "RPC"
    return f"{subject} failed with {status}"


_EVENT_MESSAGE_BUILDERS: Mapping[
    str, Callable[[DiagnosticEvent, bool], str]
] = {
    "transport.connecting": _connecting_message,
    "transport.connected": _connected_message,
    "schema.source": _schema_source_message,
    "reflection.negotiated": _reflection_message,
    "rpc.resolved": _resolved_message,
    "rpc.started": _started_message,
    "rpc.lifecycle.completed": _lifecycle_message,
    "rpc.completed": _completed_message,
    "rpc.failed": _failed_message,
}


def event_message(event: DiagnosticEvent, *, sanitize: bool = True) -> str:
    """Return semantic prose for known events without dumping fields."""
    builder = _EVENT_MESSAGE_BUILDERS.get(event.name)
    if builder is not None:
        return builder(event, sanitize)
    if event.message:
        return safe(event.message) if sanitize else event.message
    generated = event.name.replace(".", " ")
    return safe(generated) if sanitize else generated
