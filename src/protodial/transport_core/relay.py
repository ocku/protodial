"""Bounded nonblocking byte forwarding between local and TLS sockets."""

from __future__ import annotations

import contextlib
import selectors
import socket
import ssl
import threading
from collections.abc import Container


class _RelayDirection:
    def __init__(
        self, source: socket.socket, destination: socket.socket
    ) -> None:
        self.source = source
        self.destination = destination
        self.buffer = b""
        self.eof = False
        self.read_wants_write = False
        self.write_wants_read = False

    def readiness(self) -> tuple[socket.socket, int, socket.socket, int]:
        source_events = 0
        destination_events = 0
        if not self.eof and not self.buffer:
            source_events |= (
                selectors.EVENT_WRITE
                if self.read_wants_write
                else selectors.EVENT_READ
            )
        if self.buffer:
            destination_events |= (
                selectors.EVENT_READ
                if self.write_wants_read
                else selectors.EVENT_WRITE
            )
        return self.source, source_events, self.destination, destination_events

    def has_pending_data(self) -> bool:
        return (
            not self.eof
            and not self.buffer
            and isinstance(self.source, ssl.SSLSocket)
            and self.source.pending() > 0
        )

    def transfer(
        self,
        readable: Container[object],
        writable: Container[object],
        buffer_size: int,
    ) -> None:
        if self.buffer:
            ready = (
                self.destination in readable
                if self.write_wants_read
                else self.destination in writable
            )
            if ready:
                self._write()
        elif not self.eof:
            ready = (
                self.source in writable
                if self.read_wants_write
                else self.source in readable
            )
            if ready or self.has_pending_data():
                self._read(buffer_size)

    def _write(self) -> None:
        try:
            sent = self.destination.send(self.buffer)
            if sent == 0:
                raise ConnectionError("tunnel socket write returned zero")
            self.buffer = self.buffer[sent:]
            self.write_wants_read = False
        except ssl.SSLWantReadError:
            self.write_wants_read = True
        except (ssl.SSLWantWriteError, BlockingIOError):
            self.write_wants_read = False

    def _read(self, buffer_size: int) -> None:
        try:
            self.buffer = self.source.recv(buffer_size)
            self.read_wants_write = False
            if not self.buffer:
                self.eof = True
                with contextlib.suppress(OSError):
                    self.destination.shutdown(socket.SHUT_WR)
        except ssl.SSLWantWriteError:
            self.read_wants_write = True
        except (ssl.SSLWantReadError, BlockingIOError):
            self.read_wants_write = False


class SocketRelay:
    """Forward both directions with bounded buffers and TLS retry handling."""

    def __init__(
        self,
        client: socket.socket,
        remote: socket.socket,
        buffer_size: int,
        closed: threading.Event,
    ) -> None:
        self.directions = (
            _RelayDirection(client, remote),
            _RelayDirection(remote, client),
        )
        self.buffer_size = buffer_size
        self.closed = closed

    def run(self) -> None:
        """Relay until peers finish, shutdown is requested, or I/O fails."""
        connections = {direction.source for direction in self.directions} | {
            direction.destination for direction in self.directions
        }
        for connection in connections:
            connection.setblocking(False)

        with selectors.DefaultSelector() as selector:
            while not self.closed.is_set():
                interests = self._interests()
                self._update_selector(selector, interests)
                if not any(interests.values()):
                    return

                pending = any(
                    direction.has_pending_data()
                    for direction in self.directions
                )
                events = selector.select(0 if pending else 0.1)
                readable = {
                    key.fileobj
                    for key, mask in events
                    if mask & selectors.EVENT_READ
                }
                writable = {
                    key.fileobj
                    for key, mask in events
                    if mask & selectors.EVENT_WRITE
                }
                for direction in self.directions:
                    direction.transfer(readable, writable, self.buffer_size)

    def _interests(self) -> dict[socket.socket, int]:
        interests: dict[socket.socket, int] = {}
        for direction in self.directions:
            source, source_events, destination, destination_events = (
                direction.readiness()
            )
            interests[source] = interests.get(source, 0) | source_events
            interests[destination] = (
                interests.get(destination, 0) | destination_events
            )
        return interests

    @staticmethod
    def _update_selector(
        selector: selectors.BaseSelector,
        interests: dict[socket.socket, int],
    ) -> None:
        for connection, events in interests.items():
            try:
                key = selector.get_key(connection)
            except KeyError:
                if events:
                    selector.register(connection, events)
            else:
                if events and events != key.events:
                    selector.modify(connection, events)
                elif not events:
                    selector.unregister(connection)
