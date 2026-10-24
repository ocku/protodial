"""Transport mode, channel option, and compression handling."""

from __future__ import annotations

from collections.abc import Sequence

from .protocols import ChannelOption

_MODES = {"plaintext", "verified", "insecure"}


def transport_config(command_or_transport: object) -> object:
    """Extract transport configuration from a command or return the input."""
    return getattr(command_or_transport, "transport", command_or_transport)


def connection_config(command_or_connection: object) -> object:
    """Extract connection configuration from a command or return the input."""
    return getattr(command_or_connection, "connection", command_or_connection)


def transport_mode(command_or_transport: object) -> str:
    """Return the validated TLS mode for a command or transport config."""
    transport = transport_config(command_or_transport)
    mode = getattr(transport, "mode", None)
    if mode is None:
        mode = (
            "plaintext"
            if getattr(transport, "plaintext", False)
            else "verified"
        )
    if mode not in _MODES:
        choices = ", ".join(sorted(_MODES))
        raise ValueError(f"TLS mode must be one of: {choices}")
    return mode


def positive_or_unlimited(value: object, label: str) -> int | None:
    """Validate a positive byte limit or the unlimited sentinel."""
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value == 0
        or value < -1
    ):
        raise ValueError(f"{label} must be positive or -1 for unlimited")
    return value


def build_channel_options(
    command_or_transport: object, connection: object | None = None
) -> list[ChannelOption]:
    """Build documented grpcio channel options and validate their bounds."""
    transport = transport_config(command_or_transport)
    if connection is None:
        connection = getattr(command_or_transport, "connection", None)
    options: list[ChannelOption] = []

    server_name = getattr(transport, "server_name", None)
    authority = getattr(transport, "authority", None)
    if server_name == "" or authority == "":
        raise ValueError("server name and authority must not be empty")
    if server_name:
        options.append(("grpc.ssl_target_name_override", server_name))
    if authority:
        options.append(("grpc.default_authority", authority))

    if connection is None:
        return options
    receive = positive_or_unlimited(
        getattr(connection, "max_recv_message_size", None),
        "maximum receive message size",
    )
    send = positive_or_unlimited(
        getattr(connection, "max_send_message_size", None),
        "maximum send message size",
    )
    if receive is not None:
        options.append(("grpc.max_receive_message_length", receive))
    if send is not None:
        options.append(("grpc.max_send_message_length", send))

    keepalive = getattr(connection, "keepalive_time", None)
    if keepalive is not None:
        if (
            isinstance(keepalive, bool)
            or not isinstance(keepalive, (int, float))
            or keepalive <= 0
        ):
            raise ValueError("keepalive time must be positive")
        milliseconds = round(keepalive * 1000)
        if milliseconds <= 0:
            raise ValueError("keepalive time is too small")
        options.append(("grpc.keepalive_time_ms", milliseconds))

    user_agent = getattr(connection, "user_agent", None)
    if user_agent == "":
        raise ValueError("user agent must not be empty")
    if user_agent is not None:
        options.append(("grpc.primary_user_agent", user_agent))
    return options


def map_compression(grpc: object, value: str | None) -> object:
    """Map the CLI spelling to grpcio's public Compression enum."""
    normalized = "none" if value is None else value.lower()
    names = {"none": "NoCompression", "gzip": "Gzip", "deflate": "Deflate"}
    member_name = names.get(normalized)
    compression = getattr(grpc, "Compression", None)
    member = (
        getattr(compression, member_name, None)
        if member_name is not None
        else None
    )
    if member is None:
        supported = [
            name for name, attr in names.items() if hasattr(compression, attr)
        ]
        raise ValueError(
            f"unsupported compression {value!r}; choose from: "
            f"{', '.join(supported)}"
        )
    return member


def replace_option(
    options: Sequence[ChannelOption], key: str, value: int | str
) -> list[ChannelOption]:
    """Replace one channel option while preserving all other options."""
    return [(name, item) for name, item in options if name != key] + [
        (key, value)
    ]
