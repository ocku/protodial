"""Bounded local tunnel for explicit, unverified TLS connections.

grpcio does not expose a public way to disable certificate verification.  This
module therefore terminates the unverified TLS connection in a small local
proxy and gives grpcio a plaintext endpoint. On platforms that support Unix
sockets, the listener is accessible only to the invoking user.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import tempfile
import threading
from collections.abc import Callable
from typing import TextIO, cast

from google.protobuf.message import Message

from ..errors import add_secondary_error
from ..rpc_core.protocols import (
    StreamStreamCallable,
    StreamUnaryCallable,
    UnaryStreamCallable,
    UnaryUnaryCallable,
)
from .protocols import Channel, Tunnel


def _unix_socket_directory() -> tempfile.TemporaryDirectory[str]:
    """Create a private directory with a path short enough for Unix sockets."""
    system_tmp = os.path.realpath("/tmp")
    root = (
        system_tmp
        if os.path.isdir(system_tmp) and os.access(system_tmp, os.W_OK)
        else None
    )
    return tempfile.TemporaryDirectory(prefix="protodial-", dir=root)


class InsecureTlsTunnel:
    """Relay private local connections to an unverified TLS endpoint.

    Unix platforms use a socket in a private temporary directory and restrict
    the socket to the invoking user. Other platforms use loopback TCP. The TCP
    fallback rejects client credentials because another local process could
    otherwise connect to the relay and use the client's identity.
    """

    def __init__(
        self,
        target: str,
        port: int | None = None,
        *,
        server_name: str | None = None,
        connect_timeout: float | None = None,
        certfile: str | None = None,
        keyfile: str | None = None,
        certificate_chain: bytes | None = None,
        private_key: bytes | None = None,
        buffer_size: int = 64 * 1024,
        max_connections: int = 32,
        warning_stream: TextIO | None = None,
        warning_callback: Callable[[], None] | None = None,
    ) -> None:
        if port is None:
            self.remote_host, self.remote_port = split_host_port(target)
        else:
            self.remote_host, self.remote_port = target, port
        self.server_name = server_name or self.remote_host
        self.connect_timeout = connect_timeout
        self.certfile = certfile
        self.keyfile = keyfile
        self.certificate_chain = certificate_chain
        self.private_key = private_key
        if (certificate_chain is None) != (private_key is None):
            raise ValueError(
                "client certificate and private key must be used together"
            )
        if certificate_chain is not None and (
            certfile is not None or keyfile is not None
        ):
            raise ValueError(
                "client TLS material must be provided as paths or bytes, not "
                "both"
            )
        if buffer_size <= 0:
            raise ValueError("tunnel buffer size must be positive")
        if max_connections <= 0:
            raise ValueError("tunnel connection limit must be positive")
        self.buffer_size = buffer_size
        self.max_connections = max_connections
        self.warning_stream = warning_stream
        self.warning_callback = warning_callback

        self._listener: socket.socket | None = None
        self._listener_directory: tempfile.TemporaryDirectory[str] | None = None
        self._listener_path: str | None = None
        self._use_unix_socket = hasattr(socket, "AF_UNIX")
        self._ssl_context: ssl.SSLContext | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_connections)
        self._closed = threading.Event()
        self._error_ready = threading.Event()
        self._last_error: BaseException | None = None

    @property
    def target(self) -> str:
        """Return the local endpoint exposed to grpcio."""
        listener = self._listener
        if listener is None:
            raise RuntimeError("insecure TLS tunnel has not been started")
        if self._listener_path is not None:
            return f"unix:{self._listener_path}"
        host, port = listener.getsockname()[:2]
        return f"{host}:{port}"

    @property
    def last_error(self) -> BaseException | None:
        """Return the most recent asynchronous tunnel error, if any."""
        with self._lock:
            return self._last_error

    def wait_for_error(
        self, timeout: float | None = None
    ) -> BaseException | None:
        """Wait up to *timeout* seconds for an asynchronous tunnel error."""
        self._error_ready.wait(timeout)
        return self.last_error

    def _create_listener(
        self,
    ) -> tuple[
        socket.socket,
        tempfile.TemporaryDirectory[str] | None,
        str | None,
    ]:
        listener: socket.socket | None = None
        directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            if self._use_unix_socket:
                directory = _unix_socket_directory()
                path = os.path.join(directory.name, "relay.sock")
                unix_family = cast(int, getattr(socket, "AF_UNIX"))
                listener = socket.socket(unix_family, socket.SOCK_STREAM)
                listener.bind(path)
                os.chmod(path, 0o600)
                return listener, directory, path

            if self.certificate_chain is not None or self.certfile is not None:
                raise RuntimeError(
                    "insecure TLS with client credentials requires Unix "
                    "socket support"
                )
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            return listener, None, None
        except BaseException as primary:
            self._cleanup_start_resources(primary, listener, directory)
            raise

    def _emit_insecure_warning(self) -> None:
        if self.warning_callback is not None:
            self.warning_callback()
            return

        from ..presentation.diagnostics.models import (
            DiagnosticEvent,
            DiagnosticSeverity,
        )
        from ..presentation.diagnostics.rendering import StderrDiagnosticSink

        StderrDiagnosticSink(0, stream=self.warning_stream).emit(
            DiagnosticEvent(
                "transport.insecure",
                (
                    "TLS certificate verification is disabled "
                    f"for `{self.remote_host}:{self.remote_port}`"
                ),
                level=0,
                severity=DiagnosticSeverity.WARNING,
                code="W3001",
                notes=(
                    "traffic is encrypted, but the server's identity is not "
                    "verified",
                ),
                help=(
                    "remove `--insecure` or provide a trusted CA with "
                    "`--cacert <path>`",
                ),
            )
        )

    @staticmethod
    def _cleanup_start_resources(
        primary: BaseException,
        listener: socket.socket | None,
        directory: tempfile.TemporaryDirectory[str] | None,
    ) -> None:
        if listener is not None:
            try:
                listener.close()
            except BaseException as cleanup_error:
                add_secondary_error(
                    primary,
                    "tunnel listener cleanup also failed",
                    cleanup_error,
                )
        if directory is not None:
            try:
                directory.cleanup()
            except BaseException as cleanup_error:
                add_secondary_error(
                    primary,
                    "tunnel directory cleanup also failed",
                    cleanup_error,
                )

    def _reset_failed_start(self) -> None:
        self._closed.set()
        self._ssl_context = None
        self._listener = None
        self._accept_thread = None
        self._listener_path = None
        self._listener_directory = None

    def start(self) -> InsecureTlsTunnel:
        """Start the local relay and return this tunnel."""
        listener: socket.socket | None = None
        listener_directory: tempfile.TemporaryDirectory[str] | None = None
        with self._lock:
            if self._listener is not None:
                return self
            if self._closed.is_set():
                raise RuntimeError("insecure TLS tunnel is closed")
            try:
                context = self._create_ssl_context()
                listener, listener_directory, listener_path = (
                    self._create_listener()
                )
                listener.listen(min(self.max_connections, socket.SOMAXCONN))
                listener.settimeout(0.2)
                accept_thread = threading.Thread(
                    target=self._accept_loop,
                    name="protodial-insecure-tls-accept",
                    daemon=True,
                )
                self._ssl_context = context
                self._listener = listener
                self._listener_path = listener_path
                self._listener_directory = listener_directory
                self._emit_insecure_warning()
                accept_thread.start()
                self._accept_thread = accept_thread
            except BaseException as primary:
                self._reset_failed_start()
                self._cleanup_start_resources(
                    primary, listener, listener_directory
                )
                raise
        return self

    def close(self) -> None:
        """Stop the relay and release all listener and connection resources."""
        with self._lock:
            self._closed.set()
            listener, self._listener = self._listener, None
            listener_directory, self._listener_directory = (
                self._listener_directory,
                None,
            )
            self._listener_path = None
            accept_thread = self._accept_thread
            sockets = tuple(self._sockets)
            workers = tuple(self._workers)

        cleanup_errors: list[tuple[str, BaseException]] = []

        def attempt(description: str, action: Callable[[], object]) -> None:
            try:
                action()
            except BaseException as error:
                cleanup_errors.append((description, error))

        if listener is not None:
            attempt("tunnel listener cleanup", listener.close)
        if listener_directory is not None:
            attempt("tunnel directory cleanup", listener_directory.cleanup)
        for connection in sockets:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            attempt("tunnel socket cleanup", connection.close)
        current = threading.current_thread()
        if accept_thread is not None and accept_thread is not current:
            attempt(
                "tunnel accept-thread cleanup",
                lambda: accept_thread.join(timeout=1.0),
            )
        for worker in workers:
            if worker is not current:
                attempt(
                    "tunnel worker-thread cleanup",
                    lambda worker=worker: worker.join(timeout=1.0),
                )

        if cleanup_errors:
            _, primary = cleanup_errors[0]
            for description, error in cleanup_errors[1:]:
                add_secondary_error(
                    primary, f"{description} also failed", error
                )
            raise primary

    def __enter__(self) -> InsecureTlsTunnel:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

    def _record_error(self, error: BaseException) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._last_error = error
            self._error_ready.set()

    def _accept_loop(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._closed.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if not self._closed.is_set():
                    self._record_error(error)
                return
            if self._closed.is_set():
                client.close()
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                self._record_error(
                    ConnectionError(
                        "insecure TLS tunnel connection limit reached"
                    )
                )
                continue
            try:
                worker = threading.Thread(
                    target=self._serve,
                    args=(client,),
                    name="protodial-insecure-tls-relay",
                    daemon=True,
                )
                with self._lock:
                    if self._closed.is_set():
                        should_start = False
                    else:
                        self._workers.add(worker)
                        self._sockets.add(client)
                        try:
                            worker.start()
                        except BaseException:
                            self._workers.discard(worker)
                            self._sockets.discard(client)
                            raise
                        should_start = True
            except BaseException as error:
                with contextlib.suppress(OSError):
                    client.close()
                self._slots.release()
                self._record_error(error)
                if self._closed.is_set():
                    return
                continue
            if not should_start:
                with contextlib.suppress(OSError):
                    client.close()
                self._slots.release()
                return

    def _serve(self, client: socket.socket) -> None:
        raw_remote: socket.socket | None = None
        remote: socket.socket | None = None
        try:
            raw_remote = socket.create_connection(
                (self.remote_host, self.remote_port),
                timeout=self.connect_timeout,
            )
            if not self._track_socket(raw_remote):
                return
            context = self._ssl_context
            if context is None:
                raise RuntimeError("insecure TLS tunnel has not been started")
            remote = context.wrap_socket(
                raw_remote, server_hostname=self.server_name
            )
            if not self._replace_socket(raw_remote, remote):
                return
            if remote.selected_alpn_protocol() != "h2":
                raise ssl.SSLError(
                    "TLS server did not negotiate the h2 ALPN protocol"
                )
            remote.settimeout(None)
            self._relay_bidirectionally(client, remote)
        except (OSError, ssl.SSLError) as error:
            self._record_error(error)
        finally:
            for connection in (client, remote or raw_remote):
                if connection is not None:
                    self._untrack_socket(connection)
                    with contextlib.suppress(OSError):
                        connection.close()
            with self._lock:
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _create_ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(["h2"])
        if self.certificate_chain is not None and self.private_key is not None:
            with tempfile.TemporaryDirectory(
                prefix="protodial-client-tls-"
            ) as directory:
                cert_path = os.path.join(directory, "certificate.pem")
                key_path = os.path.join(directory, "private-key.pem")
                _write_private_file(cert_path, self.certificate_chain)
                _write_private_file(key_path, self.private_key)
                context.load_cert_chain(cert_path, key_path)
        elif self.certfile is not None:
            context.load_cert_chain(self.certfile, self.keyfile)
        return context

    def _track_socket(self, connection: socket.socket) -> bool:
        with self._lock:
            if self._closed.is_set():
                return False
            self._sockets.add(connection)
            return True

    def _replace_socket(self, old: socket.socket, new: socket.socket) -> bool:
        with self._lock:
            self._sockets.discard(old)
            if self._closed.is_set():
                return False
            self._sockets.add(new)
            return True

    def _untrack_socket(self, connection: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(connection)

    def _relay_bidirectionally(
        self, client: socket.socket, remote: socket.socket
    ) -> None:
        errors: list[BaseException] = []
        error_lock = threading.Lock()

        def relay(source: socket.socket, destination: socket.socket) -> None:
            try:
                while not self._closed.is_set():
                    data = source.recv(self.buffer_size)
                    if not data:
                        with contextlib.suppress(OSError):
                            destination.shutdown(socket.SHUT_WR)
                        return
                    destination.sendall(data)
            except (OSError, ssl.SSLError) as error:
                if not self._closed.is_set():
                    with error_lock:
                        errors.append(error)
                with contextlib.suppress(OSError):
                    destination.shutdown(socket.SHUT_RDWR)

        outbound = threading.Thread(
            target=relay,
            args=(client, remote),
            name="protodial-insecure-tls-upstream",
            daemon=True,
        )
        outbound.start()
        relay(remote, client)
        outbound.join()
        if errors:
            self._record_error(errors[0])


def _write_private_file(path: str, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)


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


def split_host_port(target: str) -> tuple[str, int]:
    """Parse a TCP authority, rejecting resolver and Unix targets."""
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0 or target[closing + 1 : closing + 2] != ":":
            raise ValueError("insecure TLS target must be [IPv6]:PORT")
        host = target[1:closing]
        port_text = target[closing + 2 :]
    else:
        if "://" in target or target.count(":") != 1:
            raise ValueError("insecure TLS requires a TCP target of HOST:PORT")
        host, port_text = target.rsplit(":", 1)
    if not host:
        raise ValueError("insecure TLS target host must not be empty")
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError(
            "insecure TLS target port must be an integer"
        ) from error
    if not 1 <= port <= 65535:
        raise ValueError("insecure TLS target port must be between 1 and 65535")
    return host, port
