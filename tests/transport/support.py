from types import SimpleNamespace

from protodial.commands.models import ParsedTarget


class FakeCompression:
    NoCompression = object()
    Gzip = object()
    Deflate = object()


class FakeChannel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeGrpc:
    Compression = FakeCompression

    class FutureTimeoutError(Exception):
        pass

    def __init__(self) -> None:
        self.calls = []
        self.channel = FakeChannel()
        self.future = SimpleNamespace(
            result=lambda timeout: None, cancel=lambda: None
        )

    def insecure_channel(self, target, **kwargs):
        self.calls.append(("insecure", target, kwargs))
        return self.channel

    def secure_channel(self, target, credentials, **kwargs):
        self.calls.append(("secure", target, credentials, kwargs))
        return self.channel

    def ssl_channel_credentials(self, **kwargs):
        self.calls.append(("credentials", kwargs))
        return "credentials"

    def channel_ready_future(self, channel):
        assert channel is self.channel
        return self.future


def make_command(mode="verified", **connection_overrides):
    connection = {
        "connect_timeout": 2.5,
        "max_recv_message_size": None,
        "max_send_message_size": None,
        "compression": "none",
        "keepalive_time": None,
        "user_agent": None,
    }
    connection.update(connection_overrides)
    return SimpleNamespace(
        target=ParsedTarget(
            "example.test:443",
            authority="example.test:443",
            tunnel_host="example.test",
            tunnel_port=443,
        ),
        transport=SimpleNamespace(
            mode=mode,
            ca_paths=(),
            cert=None,
            key=None,
            key_password=None,
            server_name=None,
            authority=None,
        ),
        connection=SimpleNamespace(**connection),
    )
