"""gRPC channel construction and readiness lifecycle."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator

from ..commands.models import Command
from ..errors import add_secondary_error
from .insecure import TunnelChannel
from .options import (
    build_channel_options,
    map_compression,
    replace_option,
    transport_mode,
)
from .protocols import (
    Channel,
    ChannelOption,
    GrpcTransport,
    Tunnel,
    TunnelFactory,
)
from .tls import TlsMaterial

TlsValidator = Callable[[Command], TlsMaterial]
ChannelFactory = Callable[[Command, GrpcTransport], Channel]


def channel_arguments(
    command: Command, grpc: GrpcTransport
) -> tuple[list[ChannelOption], object]:
    """Build channel options and the selected compression value."""
    options = build_channel_options(command, command.connection)
    return options, map_compression(grpc, command.connection.compression)


def create_channel(
    command: Command,
    grpc: GrpcTransport,
    *,
    tls_validator: TlsValidator,
    tunnel_factory: TunnelFactory[Tunnel],
) -> Channel:
    """Create a plaintext, verified TLS, or explicit insecure TLS channel."""
    mode = transport_mode(command)
    material = tls_validator(command)
    options, compression = channel_arguments(command, grpc)
    channel_kwargs: dict[str, object] = {
        "options": options,
        "compression": compression,
    }

    if mode == "plaintext":
        return grpc.insecure_channel(command.target.channel, **channel_kwargs)
    if mode == "verified":
        credentials = grpc.ssl_channel_credentials(
            root_certificates=material.root_certificates,
            private_key=material.private_key,
            certificate_chain=material.certificate_chain,
        )
        return grpc.secure_channel(
            command.target.channel, credentials, **channel_kwargs
        )

    transport = command.transport
    connection = command.connection
    connect_timeout = connection.connect_timeout
    if connect_timeout <= 0:
        raise ValueError("connect timeout must be positive")
    remote_host = command.target.tunnel_host
    remote_port = command.target.tunnel_port
    if remote_host is None or remote_port is None:
        raise ValueError("insecure TLS requires a direct TCP target")
    authority = transport.authority or command.target.authority
    if not authority:
        raise ValueError("insecure TLS target has no usable authority")
    tunnel = tunnel_factory(
        remote_host,
        remote_port,
        server_name=transport.server_name,
        connect_timeout=connect_timeout,
        certificate_chain=material.certificate_chain,
        private_key=material.private_key,
    )
    tunnel.start()
    options = replace_option(options, "grpc.default_authority", authority)
    options = [
        (key, value)
        for key, value in options
        if key != "grpc.ssl_target_name_override"
    ]
    try:
        channel = grpc.insecure_channel(
            tunnel.target, options=options, compression=compression
        )
    except BaseException as primary:
        try:
            tunnel.close()
        except BaseException as cleanup_error:
            add_secondary_error(
                primary, "tunnel cleanup also failed", cleanup_error
            )
        raise
    return TunnelChannel(channel, tunnel)


def connect_timeout(command: Command) -> float | None:
    """Return a validated connection timeout."""
    timeout = getattr(command.connection, "connect_timeout", None)
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ValueError("connect timeout must be positive")
    return float(timeout)


@contextlib.contextmanager
def ready_channel(
    command: Command,
    grpc: GrpcTransport,
    *,
    channel_factory: ChannelFactory,
    await_ready: bool = True,
) -> Generator[Channel]:
    """Create, await, yield, and always close a channel."""
    timeout = connect_timeout(command)
    channel = channel_factory(command, grpc)
    future = None
    primary: BaseException | None = None
    try:
        if await_ready:
            future = grpc.channel_ready_future(channel)
            try:
                future.result(timeout)
            except BaseException as error:
                tunnel = getattr(channel, "tunnel", None)
                tunnel_error = tunnel.last_error if tunnel is not None else None
                if tunnel_error is not None:
                    raise ConnectionError(
                        f"insecure TLS connection failed: {tunnel_error}"
                    ) from tunnel_error
                timeout_type = grpc.FutureTimeoutError
                if timeout_type and isinstance(error, timeout_type):
                    raise TimeoutError(
                        f"connection was not ready within {timeout:g} seconds"
                    ) from error
                raise
        yield channel
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        if future is not None:
            try:
                future.cancel()
            except BaseException as error:
                if primary is not None:
                    add_secondary_error(
                        primary,
                        "readiness cancellation also failed",
                        error,
                    )
                else:
                    cleanup_error = error
        try:
            channel.close()
        except BaseException as error:
            if primary is not None:
                add_secondary_error(
                    primary, "channel cleanup also failed", error
                )
            elif cleanup_error is None:
                cleanup_error = error
            else:
                add_secondary_error(
                    cleanup_error, "channel cleanup also failed", error
                )
        if primary is None and cleanup_error is not None:
            raise cleanup_error
