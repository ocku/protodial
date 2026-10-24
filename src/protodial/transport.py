"""Construct and validate gRPC transports."""

from __future__ import annotations

import contextlib
import ssl
from collections.abc import Callable, Generator
from typing import cast

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .commands.models import Command
from .transport_core import channels as _channels
from .transport_core import tls as _tls
from .transport_core.insecure import InsecureTlsTunnel
from .transport_core.protocols import Channel, GrpcTransport
from .transport_core.tls import PrivateKeyLoader, TlsMaterial


def validate_tls_material(command_or_transport: object) -> TlsMaterial:
    """Read and validate configured TLS files before grpc opens a channel."""
    return _tls.validate_tls_material(
        command_or_transport,
        ssl_context_factory=ssl.SSLContext,
        default_context_factory=ssl.create_default_context,
        private_key_loader=cast(PrivateKeyLoader, load_pem_private_key),
    )


def create_channel(command: Command, grpc: GrpcTransport) -> Channel:
    """Create a plaintext, verified TLS, or explicit insecure TLS channel."""
    from .presentation.diagnostics.context import current_sink
    from .presentation.diagnostics.models import (
        DiagnosticEvent,
        DiagnosticSeverity,
    )

    sink = current_sink()
    warning_target = command.target.authority or command.target.channel

    tunnel_factory = cast(Callable[..., InsecureTlsTunnel], InsecureTlsTunnel)
    if sink is not None:
        active_sink = sink

        def tunnel_factory(
            *args: object, **kwargs: object
        ) -> InsecureTlsTunnel:
            kwargs["warning_callback"] = lambda: active_sink.emit(
                DiagnosticEvent(
                    "transport.insecure",
                    (
                        "TLS certificate verification is disabled for "
                        f"`{warning_target}`"
                    ),
                    level=0,
                    severity=DiagnosticSeverity.WARNING,
                    code="W3001",
                    fields={"target": command.target.channel},
                    help=(
                        "remove `--insecure` or provide a trusted CA with "
                        "`--cacert <path>`",
                    ),
                    notes=(
                        "traffic is encrypted, but the server's identity is "
                        "not verified",
                    ),
                )
            )
            constructor = cast(
                Callable[..., InsecureTlsTunnel], InsecureTlsTunnel
            )
            return constructor(*args, **kwargs)

    return _channels.create_channel(
        command,
        grpc,
        tls_validator=validate_tls_material,
        tunnel_factory=tunnel_factory,
    )


@contextlib.contextmanager
def ready_channel(
    command: Command,
    grpc: GrpcTransport,
    *,
    await_ready: bool = True,
) -> Generator[Channel]:
    """Create, await, yield, and always close a channel."""
    with _channels.ready_channel(
        command,
        grpc,
        channel_factory=create_channel,
        await_ready=await_ready,
    ) as channel:
        yield channel


__all__ = [
    "create_channel",
    "ready_channel",
    "validate_tls_material",
]
