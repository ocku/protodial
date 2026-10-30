"""Channel proxy owning the lifecycle of a local TLS tunnel."""

from __future__ import annotations

from collections.abc import Callable

from google.protobuf.message import Message

from ..errors import add_secondary_error
from ..rpc_core.protocols import (
    StreamStreamCallable,
    StreamUnaryCallable,
    UnaryStreamCallable,
    UnaryUnaryCallable,
)
from .protocols import Channel, Tunnel


class TunnelChannel:
    """Channel proxy that owns and closes its insecure TLS tunnel."""

    def __init__(self, channel: Channel, tunnel: Tunnel) -> None:
        self._channel = channel
        self.tunnel = tunnel

    def __getattr__(self, name: str) -> object:
        return getattr(self._channel, name)

    def unary_unary(
        self, method: str, **kwargs: object
    ) -> UnaryUnaryCallable[Message, Message]:
        """Create a unary-request, unary-response callable."""
        return self._channel.unary_unary(method, **kwargs)

    def unary_stream(
        self, method: str, **kwargs: object
    ) -> UnaryStreamCallable[Message, Message]:
        """Create a unary-request, streaming-response callable."""
        return self._channel.unary_stream(method, **kwargs)

    def stream_unary(
        self, method: str, **kwargs: object
    ) -> StreamUnaryCallable[Message, Message]:
        """Create a streaming-request, unary-response callable."""
        return self._channel.stream_unary(method, **kwargs)

    def stream_stream(
        self, method: str, **kwargs: object
    ) -> StreamStreamCallable[Message, Message]:
        """Create a bidirectional streaming callable."""
        return self._channel.stream_stream(method, **kwargs)

    def close(self) -> None:
        """Close the wrapped channel and its owned tunnel."""
        try:
            close: Callable[[], object] = getattr(self._channel, "close")
            close()
        except BaseException as primary:
            try:
                self.tunnel.close()
            except BaseException as cleanup_error:
                add_secondary_error(
                    primary,
                    "tunnel cleanup also failed",
                    cleanup_error,
                )
            raise
        else:
            self.tunnel.close()

    def __enter__(self) -> TunnelChannel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
