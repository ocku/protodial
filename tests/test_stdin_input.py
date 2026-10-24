import io
import os
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from protodial.rpc_core.input import _StdinReader, open_input


@pytest.mark.parametrize("fail", [False, True])
def test_close_defers_owned_descriptor_cleanup_during_read(fail: bool) -> None:
    entered = Event()
    release = Event()
    closed = Event()
    errors: list[BaseException] = []

    class BlockingInput(io.BytesIO):
        def readinto(self, buffer: object) -> int:
            entered.set()
            assert release.wait(5)
            if fail:
                raise OSError("read failed")
            return 0

        def close(self) -> None:
            # Models the CRT descriptor lock: closing during a read blocks.
            assert release.is_set()
            closed.set()
            super().close()

    source = BlockingInput()
    reader = _StdinReader(source)

    def read() -> None:
        try:
            reader.read(1)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=read, daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        reader.close()
        reader.close()
        assert reader.closed
        assert not closed.is_set()
        with pytest.raises(ValueError, match="closed"):
            reader.read(1)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert closed.is_set()
    assert len(errors) == int(fail)
    if fail:
        assert isinstance(errors[0], OSError)
        assert str(errors[0]) == "read failed"


def test_stdin_duplicate_preserves_binary_reads_and_original_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as stdin:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stdin))
        os.write(write_fd, b"one\r\ntwo\x00\xff")
        os.close(write_fd)
        with open_input("@-") as source:
            assert source.readline() == b"one\r\n"
            assert source.read(3) == b"two"
            assert source.read() == b"\x00\xff"
            assert source.read() == b""
        assert source.closed
        assert not stdin.closed
        assert stdin.read() == b""


def test_stdin_reader_closes_idle_source() -> None:
    source = io.BytesIO(b"data")
    reader = _StdinReader(source)
    reader.close()
    reader.close()
    assert reader.closed
    assert source.closed
