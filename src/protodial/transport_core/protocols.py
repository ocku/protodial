"""Focused structural types used by the transport implementation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeAlias, TypeVar

from ..rpc_core.protocols import RpcChannel

ChannelOption: TypeAlias = tuple[str, int | str]
ChannelOptions: TypeAlias = Sequence[ChannelOption]


class ReadinessFuture(Protocol):
    """Wait for or cancel grpcio channel readiness."""

    def result(self, timeout: float | None = None) -> object:
        """Wait until the channel is ready or the timeout expires."""
        ...

    def cancel(self) -> object:
        """Cancel the readiness wait."""
        ...


class Channel(RpcChannel, Protocol):
    """Provide dynamic RPC factories and channel lifecycle control."""

    def close(self) -> object:
        """Close the channel."""
        ...


class GrpcTransport(Protocol):
    """Provide the grpcio transport surface used by channel construction."""

    Compression: object
    FutureTimeoutError: type[BaseException]

    def insecure_channel(self, target: str, **kwargs: object) -> Channel:
        """Create a plaintext channel."""
        ...

    def secure_channel(
        self, target: str, credentials: object, **kwargs: object
    ) -> Channel:
        """Create a TLS channel."""
        ...

    def ssl_channel_credentials(self, **kwargs: bytes | None) -> object:
        """Create TLS channel credentials."""
        ...

    def channel_ready_future(self, channel: object) -> ReadinessFuture:
        """Return a future tracking channel readiness."""
        ...


class Tunnel(Protocol):
    """Expose lifecycle and status for a local transport tunnel."""

    @property
    def target(self) -> str:
        """Return the local channel target."""
        ...

    @property
    def last_error(self) -> BaseException | None:
        """Return the most recent tunnel error, if any."""
        ...

    def start(self) -> object:
        """Start the tunnel."""
        ...

    def close(self) -> None:
        """Close the tunnel."""
        ...


_TunnelT = TypeVar("_TunnelT", bound=Tunnel)
TunnelFactory: TypeAlias = Callable[..., _TunnelT]
