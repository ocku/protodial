from __future__ import annotations

import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import protodial.cli as cli_module
import protodial.completion._reflection as reflection_module
import protodial.completion.engine as completion_engine
import protodial.schema.factory as schema_factory_module
import protodial.transport as transport_module
from protodial.completion.engine import complete

from .support import (
    PROTO,
    _compiled_source,
    _environment,
    _mkfifo_or_skip,
    _saved_proto,
    _symlink_or_skip,
    _values,
)


def test_reflected_symbol_enumeration_fetches_when_file_mapping_is_empty(
    tmp_path: Path,
) -> None:
    compiled = _compiled_source(tmp_path, "reflected.proto", PROTO)

    class LazyReflectionLike:
        file_protos: dict[str, object] = {}
        pool = compiled.pool  # type: ignore[attr-defined]

        def list_services(self) -> object:
            return compiled.list_services()  # type: ignore[attr-defined]

        def find_symbol(self, name: str) -> object:
            return compiled.find_symbol(name)  # type: ignore[attr-defined]

    values = _values(
        completion_engine._symbols(
            [LazyReflectionLike()]  # type: ignore[list-item]
        )
    )
    assert "saved.example.Greeter" in values
    assert "saved.example.Greeter/SayHello" in values
    assert "saved.example.Request" in values
    assert "saved.example.Request.display_name" in values


def test_invocation_url_merges_reflection_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, proto = _saved_proto(tmp_path)
    scan = completion_engine._scan(["--proto", str(proto), ""])
    source = completion_engine._local_sources(scan, environment)[0]
    opened = completion_engine._OpenedReflectionSource(source, nullcontext())
    monkeypatch.setattr(completion_engine, "_local_sources", lambda *_: [])
    monkeypatch.setattr(
        completion_engine, "_reflection_source", lambda *_: opened
    )
    candidates = complete(
        ["grpc://localhost:50051/saved."],
        environment=environment,
        cwd=tmp_path,
    )
    assert "grpc://localhost:50051/saved.example.Greeter/SayHello" in _values(
        candidates
    )


def test_tls_reflection_uses_verified_mode_and_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def ready_channel(
        command: object, _grpc: object, *, await_ready: bool = True
    ) -> object:
        captured["command"] = command
        captured["await_ready"] = await_ready
        return nullcontext(object())

    monkeypatch.setattr(
        cli_module,
        "load_runtime",
        lambda: SimpleNamespace(grpc=SimpleNamespace()),
    )
    monkeypatch.setattr(transport_module, "ready_channel", ready_channel)
    monkeypatch.setattr(
        schema_factory_module,
        "build_descriptor_source",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    scan = completion_engine._scan(
        [
            "--connect-timeout",
            "999",
            "--max-time=999",
            "grpcs://example.test:443/",
        ]
    )
    environment = {
        **_environment(tmp_path),
        "PROTODIAL_COMPLETION_TIMEOUT": "0.25",
    }
    opened = completion_engine._reflection_source(scan, environment)
    assert opened is not None
    opened.close()
    command = captured["command"]
    assert captured["await_ready"] is False
    assert command.transport.mode == "verified"  # type: ignore[attr-defined]
    assert 0 < command.connection.connect_timeout <= 0.25  # type: ignore[attr-defined]
    assert 0 < command.timeout <= 0.25  # type: ignore[attr-defined]


def test_reflection_completion_uses_a_short_default_deadline() -> None:
    assert reflection_module._completion_timeout({}) == pytest.approx(0.2)
    assert reflection_module._completion_timeout(
        {"PROTODIAL_COMPLETION_TIMEOUT": "invalid"}
    ) == pytest.approx(0.2)


@pytest.mark.parametrize(
    "words",
    [
        ["--plaintext", "grpcs://example.test:443/"],
        ["--insecure", "grpc://example.test:80/"],
        ["--insecure", "--cacert", "ca.pem", "example.test:443"],
        ["--cert", "client.pem", "grpcs://example.test:443/"],
        ["--plaintext", "--insecure", "example.test:443"],
    ],
)
def test_invalid_secure_completion_never_opens_reflection_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    words: list[str],
) -> None:
    channels = 0

    def ready_channel(*_args: object) -> object:
        nonlocal channels
        channels += 1
        return nullcontext(object())

    monkeypatch.setattr(
        cli_module,
        "load_runtime",
        lambda: SimpleNamespace(grpc=SimpleNamespace()),
    )
    monkeypatch.setattr(transport_module, "ready_channel", ready_channel)
    scan = completion_engine._scan(
        ["--reflect-header", "authorization: secret", *words]
    )
    assert (
        completion_engine._reflection_source(scan, _environment(tmp_path))
        is None
    )
    assert channels == 0


def test_reflection_rejects_fifo_tls_input_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate = tmp_path / "ca.pem"
    _mkfifo_or_skip(certificate)
    channels = 0

    def ready_channel(*_args: object) -> object:
        nonlocal channels
        channels += 1
        return nullcontext(object())

    monkeypatch.setattr(transport_module, "ready_channel", ready_channel)
    scan = completion_engine._scan(
        ["--cacert", str(certificate), "grpcs://example.test:443/"]
    )
    started = time.monotonic()
    assert (
        completion_engine._reflection_source(scan, _environment(tmp_path))
        is None
    )
    assert time.monotonic() - started < 0.5
    assert channels == 0


def test_reflection_snapshots_a_symlinked_tls_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "real-ca.pem"
    target.write_bytes(b"test CA bytes")
    linked = tmp_path / "ca.pem"
    _symlink_or_skip(linked, target)
    observed: dict[str, object] = {}

    def ready_channel(
        command: object, _grpc: object, *, await_ready: bool = True
    ) -> object:
        snapshot = Path(command.transport.ca_paths[0])  # type: ignore[attr-defined]
        observed["path"] = snapshot
        observed["data"] = snapshot.read_bytes()
        observed["await_ready"] = await_ready
        return nullcontext(object())

    monkeypatch.setattr(
        cli_module,
        "load_runtime",
        lambda: SimpleNamespace(grpc=SimpleNamespace()),
    )
    monkeypatch.setattr(transport_module, "ready_channel", ready_channel)
    monkeypatch.setattr(
        schema_factory_module,
        "build_descriptor_source",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    scan = completion_engine._scan(
        ["--cacert", str(linked), "grpcs://example.test:443/"]
    )
    opened = completion_engine._reflection_source(scan, _environment(tmp_path))
    assert opened is not None
    snapshot = observed["path"]
    assert isinstance(snapshot, Path)
    assert snapshot != linked
    assert observed["data"] == b"test CA bytes"
    assert observed["await_ready"] is False
    assert snapshot.is_file()
    opened.close()
    assert not snapshot.exists()


def test_reflection_rejects_oversized_tls_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate = tmp_path / "large-ca.pem"
    certificate.write_bytes(b"x" * (1024 * 1024 + 1))
    channels = 0

    def ready_channel(*_args: object) -> object:
        nonlocal channels
        channels += 1
        return nullcontext(object())

    monkeypatch.setattr(transport_module, "ready_channel", ready_channel)
    scan = completion_engine._scan(
        ["--cacert", str(certificate), "grpcs://example.test:443/"]
    )
    started = time.monotonic()
    assert (
        completion_engine._reflection_source(scan, _environment(tmp_path))
        is None
    )
    assert time.monotonic() - started < 0.5
    assert channels == 0


def test_reflected_method_walk_shares_one_completion_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, proto = _saved_proto(tmp_path)
    compiled = completion_engine._local_sources(
        completion_engine._scan(["--proto", str(proto), ""]), environment
    )[0]
    service = compiled.find_symbol("saved.example.Greeter")

    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)

    class SlowReflectionLike:
        pool = compiled.pool

        def __init__(self) -> None:
            self.calls = 0

        def list_services(self) -> object:
            return tuple(f"slow.Service{index}" for index in range(20))

        def find_symbol(self, _name: str) -> object:
            nonlocal now
            self.calls += 1
            now += 0.03
            return service

    source = SlowReflectionLike()
    deadline = time.monotonic() + 0.12
    completion_engine._OpenedReflectionSource(
        source,  # type: ignore[arg-type]
        nullcontext(),
        deadline,
    )
    completion_engine._methods([source])  # type: ignore[list-item]
    assert source.calls == 4
