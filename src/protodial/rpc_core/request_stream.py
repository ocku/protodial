"""Track client request consumption and preserve producer failures."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Event
from typing import Generic, TypeVar

from .models import MessageCounter

_MessageT = TypeVar("_MessageT")
_RequestT = TypeVar("_RequestT")


def count_messages(
    iterator: Iterator[_MessageT], counter: MessageCounter
) -> Iterator[_MessageT]:
    """Yield messages while updating a shared counter."""
    for message in iterator:
        counter.value += 1
        yield message


@dataclass
class RequestStream(Generic[_RequestT], Iterator[_RequestT]):
    """Track a client stream without joining the request-consumer thread."""

    iterator: Iterator[_RequestT]
    counter: MessageCounter
    completed: Event = field(default_factory=Event)
    stopped: Event = field(default_factory=Event)
    error: BaseException | None = None

    def __iter__(self) -> RequestStream[_RequestT]:
        return self

    def __next__(self) -> _RequestT:
        if self.stopped.is_set():
            self.completed.set()
            raise StopIteration
        try:
            message = next(self.iterator)
        except StopIteration:
            self.completed.set()
            raise
        except BaseException as error:
            # grpcio consumes request iterators on a worker thread and otherwise
            # replaces producer failures with a generic RPC error. Preserve the
            # original exception, including programming errors and interrupts.
            self.error = error
            self.completed.set()
            raise
        if self.stopped.is_set():
            self.completed.set()
            raise StopIteration
        self.counter.value += 1
        return message

    def stop(self) -> None:
        """Prevent another request after the RPC reaches a terminal state."""
        self.stopped.set()
