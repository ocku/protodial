from __future__ import annotations

import gc
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.protobuf import descriptor_pb2, message_factory

import protodial.cli as cli_module
import protodial.completion._reflection as reflection_module
import protodial.completion._saved_protos as saved_protos_module
import protodial.completion.engine as completion_engine
import protodial.schema.factory as schema_factory_module
import protodial.transport as transport_module
from protodial.cli import main
from protodial.completion._saved_protos import (
    ScanBudget,
    parse_proto,
    saved_proto_files,
)
from protodial.completion.command import (
    run_completion_command,
    run_completion_protocol,
)
from protodial.completion.engine import Candidate, complete
from protodial.message_io import raw_input as raw_input_module

try:
    import pty
except ImportError:  # pragma: no cover - Windows has no Unix PTYs
    pty = None  # type: ignore[assignment]

PROTO = """\
syntax = "proto3";
package saved.example;

service Greeter {
  rpc SayHello(Request) returns (Reply);
}

message Request {
  enum Mood {
    MOOD_UNSPECIFIED = 0;
    MOOD_HAPPY = 1;
  }
  string display_name = 1;
  bytes payload = 2;
  Mood mood = 3;
  bool enabled = 4;
  repeated Mood moods = 5;
  repeated bool flags = 6;
}

message Reply {
  bytes body = 1;
}
"""


def _values(candidates: list[Candidate]) -> list[str]:
    return [candidate.value for candidate in candidates]


def _environment(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}


def _mkfifo_or_skip(path: Path) -> None:
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:
        pytest.skip("FIFOs are unavailable on this platform")
    mkfifo(path)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")


def _saved_proto(tmp_path: Path) -> tuple[dict[str, str], Path]:
    environment = _environment(tmp_path)
    path = tmp_path / "data" / "protodial" / "protos" / "api.proto"
    path.parent.mkdir(parents=True)
    path.write_text(PROTO, encoding="utf-8")
    return environment, path


def test_completion_offers_commands_options_and_static_choices(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    assert {"list", "describe", "completion"} <= set(
        _values(complete([""], environment=environment, cwd=tmp_path))
    )
    assert "--proto" in _values(
        complete(["--pr"], environment=environment, cwd=tmp_path)
    )
    assert _values(
        complete(
            ["--compression", "g"],
            environment=environment,
            cwd=tmp_path,
        )
    ) == ["gzip"]
    assert _values(
        complete(
            ["--compression=g"],
            environment=environment,
            cwd=tmp_path,
        )
    ) == ["--compression=gzip"]


def test_native_completion_target_keeps_ordinary_scanner_context(
    tmp_path: Path,
) -> None:
    environment, _ = _saved_proto(tmp_path)
    scan = completion_engine._scan(
        [
            "completion",
            "--method",
            "saved.example.Greeter/SayHello",
            "--field",
            "display",
        ]
    )
    assert scan.mode == "invoke"
    assert scan.positionals == ["completion"]
    candidates = complete(
        [
            "completion",
            "--method",
            "saved.example.Greeter/SayHello",
            "--field",
            "display",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    assert "displayName=" in _values(candidates)
    assert "--proto" in _values(
        complete(
            ["completion", "--pr"],
            environment=environment,
            cwd=tmp_path,
        )
    )

    management = completion_engine._scan(["completion", "install", ""])
    assert management.mode == "completion"


def test_proto_completion_includes_cwd_and_saved_directory(
    tmp_path: Path,
) -> None:
    environment, saved = _saved_proto(tmp_path)
    (tmp_path / "local.proto").write_text(PROTO, encoding="utf-8")
    values = _values(
        complete(["--proto", ""], environment=environment, cwd=tmp_path)
    )
    assert "local.proto" in values
    assert str(saved) in values


def test_saved_proto_completes_methods_without_becoming_cli_input(
    tmp_path: Path,
) -> None:
    environment, _ = _saved_proto(tmp_path)
    candidates = complete(
        ["grpc://unused.invalid:1/"],
        environment=environment,
        cwd=tmp_path,
    )
    assert "grpc://unused.invalid:1/saved.example.Greeter/SayHello" in _values(
        candidates
    )


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["--field", ""], "displayName="),
        (["--field:file", ""], "payload="),
        (["--input-field", ""], "payload"),
        (["--output-field", ""], "body"),
        (["--field", "mood="], "mood=MOOD_HAPPY"),
        (["--field", "enabled="], "enabled=true"),
        (["--field", "moods="], 'moods=["MOOD_HAPPY"]'),
        (["--field", "flags="], "flags=[true]"),
    ],
)
def test_schema_aware_field_and_value_completion(
    tmp_path: Path, words: list[str], expected: str
) -> None:
    environment, proto = _saved_proto(tmp_path)
    prefix = [
        "--proto",
        str(proto),
        "--method",
        "saved.example.Greeter/SayHello",
    ]
    assert expected in _values(
        complete([*prefix, *words], environment=environment, cwd=tmp_path)
    )


def test_field_file_completes_path_after_bytes_field(tmp_path: Path) -> None:
    environment, proto = _saved_proto(tmp_path)
    (tmp_path / "payload.bin").write_bytes(b"payload")
    candidates = complete(
        [
            "--proto",
            str(proto),
            "--method",
            "saved.example.Greeter/SayHello",
            "--field:file",
            "payload=pay",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    assert "payload=payload.bin" in _values(candidates)


def test_repeated_field_completions_are_accepted_by_runtime_parser(
    tmp_path: Path,
) -> None:
    environment, proto = _saved_proto(tmp_path)
    source = completion_engine._local_sources(
        completion_engine._scan(["--proto", str(proto), ""]), environment
    )[0]
    method = source.find_symbol("saved.example.Greeter/SayHello")
    message_class = message_factory.GetMessageClass(method.input_type)  # type: ignore[union-attr]
    message = raw_input_module._field_template(
        message_class,
        "payload",
        (
            ("moods", '["MOOD_HAPPY"]'),
            ("flags", "[true]"),
        ),
        source.pool,
    )
    assert list(message.moods) == [1]
    assert list(message.flags) == [True]


def test_describe_and_list_complete_local_symbols(tmp_path: Path) -> None:
    environment, proto = _saved_proto(tmp_path)
    shared = ["--proto", str(proto), "unused.invalid:1"]
    services = complete(
        ["list", *shared, "saved."],
        environment=environment,
        cwd=tmp_path,
    )
    assert "saved.example.Greeter" in _values(services)
    symbols = complete(
        ["describe", *shared, "saved.example.Request."],
        environment=environment,
        cwd=tmp_path,
    )
    assert "saved.example.Request.display_name" in _values(symbols)


def test_explicit_describe_candidates_cover_every_local_symbol(
    tmp_path: Path,
) -> None:
    proto = tmp_path / "all-symbols.proto"
    proto.write_text(
        'syntax = "proto3";\n'
        "package inventory;\n"
        "service API { rpc Call(Request) returns (Reply); }\n"
        "message Request {\n"
        "  Referenced ref = 1;\n"
        "  oneof choice { string text = 2; int32 number = 3; }\n"
        "}\n"
        "message Reply {}\n"
        "message Referenced {\n"
        "  message Nested { string value = 1; }\n"
        "  enum Kind { KIND_UNSPECIFIED = 0; KIND_ONE = 1; }\n"
        "  Kind kind = 1; Nested nested = 2;\n"
        "}\n"
        "message Standalone { string value = 1; }\n"
        "enum StandaloneEnum { STANDALONE_UNSPECIFIED = 0; }\n",
        encoding="utf-8",
    )
    environment = _environment(tmp_path)
    source = completion_engine._local_sources(
        completion_engine._scan(["--proto", str(proto), ""]), environment
    )[0]
    candidates = set(_values(completion_engine._symbols([source])))
    expected = {
        "inventory.API",
        "inventory.API/Call",
        "inventory.Request",
        "inventory.Request.ref",
        "inventory.Request.choice",
        "inventory.Request.text",
        "inventory.Request.number",
        "inventory.Reply",
        "inventory.Referenced",
        "inventory.Referenced.kind",
        "inventory.Referenced.nested",
        "inventory.Referenced.Nested",
        "inventory.Referenced.Nested.value",
        "inventory.Referenced.Kind",
        "inventory.Referenced.KIND_ONE",
        "inventory.Standalone",
        "inventory.Standalone.value",
        "inventory.StandaloneEnum",
        "inventory.STANDALONE_UNSPECIFIED",
    }
    assert expected <= candidates
    for name in expected:
        assert source.find_symbol(name) is not None


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
) -> None:
    environment, proto = _saved_proto(tmp_path)
    compiled = completion_engine._local_sources(
        completion_engine._scan(["--proto", str(proto), ""]), environment
    )[0]
    service = compiled.find_symbol("saved.example.Greeter")

    class SlowReflectionLike:
        pool = compiled.pool

        def __init__(self) -> None:
            self.calls = 0

        def list_services(self) -> object:
            return tuple(f"slow.Service{index}" for index in range(20))

        def find_symbol(self, _name: str) -> object:
            self.calls += 1
            time.sleep(0.03)
            return service

    source = SlowReflectionLike()
    deadline = time.monotonic() + 0.12
    completion_engine._OpenedReflectionSource(
        source,  # type: ignore[arg-type]
        nullcontext(),
        deadline,
    )
    started = time.monotonic()
    completion_engine._methods([source])  # type: ignore[list-item]
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert 1 <= source.calls <= 4


def test_midline_cursor_uses_current_word_and_later_schema_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _ = _saved_proto(tmp_path)
    monkeypatch.setattr(
        completion_engine, "_reflection_source", lambda *_: None
    )
    words = [
        "--field",
        "mo",
        "--method",
        "saved.example.Greeter/SayHello",
        "grpc://localhost:50051",
    ]
    candidates = complete(
        words, environment=environment, cwd=tmp_path, cursor=1
    )
    assert "mood=" in _values(candidates)


def test_discovery_urls_complete_services_and_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _ = _saved_proto(tmp_path)
    monkeypatch.setattr(
        completion_engine, "_reflection_source", lambda *_: None
    )
    listed = complete(
        ["list", "grpc://localhost:50051/saved."],
        environment=environment,
        cwd=tmp_path,
    )
    assert "grpc://localhost:50051/saved.example.Greeter" in _values(listed)
    described = complete(
        ["describe", "grpc://localhost:50051/saved.example.Request."],
        environment=environment,
        cwd=tmp_path,
    )
    assert (
        "grpc://localhost:50051/saved.example.Request.display_name"
        in _values(described)
    )


def _collision_proto(field: str, enum_value: str) -> str:
    return (
        'syntax = "proto3";\n'
        "package collision;\n"
        "service API { rpc Call(Request) returns (Reply); }\n"
        "message Request {\n"
        "  enum Mood { MOOD_UNSPECIFIED = 0; "
        f"{enum_value} = 1; }}\n"
        f"  string {field} = 1;\n"
        "  Mood mood = 2;\n"
        "}\n"
        f"message Reply {{ bytes {field}_reply = 1; }}\n"
    )


def _compiled_source(tmp_path: Path, name: str, proto: str) -> object:
    path = tmp_path / name
    path.write_text(proto, encoding="utf-8")
    environment = {
        "HOME": str(tmp_path / "isolated-home"),
        "XDG_DATA_HOME": str(tmp_path / "isolated-data"),
    }
    return completion_engine._local_sources(
        completion_engine._scan(["--proto", str(path), ""]), environment
    )[0]


def _write_protoset(tmp_path: Path, name: str = "schema.protoset") -> Path:
    source = _compiled_source(tmp_path, "protoset-source.proto", PROTO)
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    for file_proto in source.file_protos.values():  # type: ignore[attr-defined]
        descriptor_set.file.add().CopyFrom(file_proto)
    path = tmp_path / name
    path.write_bytes(descriptor_set.SerializeToString())
    return path


def test_live_reflection_precedes_stale_automatic_saved_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    saved = tmp_path / "data" / "protodial" / "protos" / "stale.proto"
    saved.parent.mkdir(parents=True)
    saved.write_text(
        _collision_proto("saved_only", "MOOD_SAVED")
        + "service Legacy { rpc Old(Request) returns (Reply); }\n",
        encoding="utf-8",
    )
    reflected = _compiled_source(
        tmp_path, "live.proto", _collision_proto("live_only", "MOOD_LIVE")
    )

    def reflected_source(*_args: object) -> object:
        return completion_engine._OpenedReflectionSource(
            reflected,
            nullcontext(),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        completion_engine, "_reflection_source", reflected_source
    )
    common = ["--method", "collision.API/Call"]
    fields = complete(
        [*common, "--field", ""],
        environment=environment,
        cwd=tmp_path,
    )
    assert "liveOnly=" in _values(fields)
    assert "savedOnly=" not in _values(fields)
    enums = complete(
        [*common, "--field", "mood="],
        environment=environment,
        cwd=tmp_path,
    )
    assert "mood=MOOD_LIVE" in _values(enums)
    assert "mood=MOOD_SAVED" not in _values(enums)
    output_fields = complete(
        [*common, "--output-field", ""],
        environment=environment,
        cwd=tmp_path,
    )
    assert "liveOnlyReply" in _values(output_fields)
    assert "savedOnlyReply" not in _values(output_fields)
    methods = complete(["--method", ""], environment=environment, cwd=tmp_path)
    assert "collision.API/Call" in _values(methods)
    assert "collision.Legacy/Old" in _values(methods)
    assert _values(methods).count("collision.API/Call") == 1
    saved_fields = complete(
        ["--method", "collision.Legacy/Old", "--field", ""],
        environment=environment,
        cwd=tmp_path,
    )
    assert "savedOnly=" in _values(saved_fields)
    assert "liveOnly=" not in _values(saved_fields)


def test_explicit_local_schema_precedes_reflection_and_saved_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    saved = tmp_path / "data" / "protodial" / "protos" / "stale.proto"
    saved.parent.mkdir(parents=True)
    saved.write_text(
        _collision_proto("saved_only", "MOOD_SAVED")
        + "service Legacy { rpc Old(Request) returns (Reply); }\n",
        encoding="utf-8",
    )
    reflected = _compiled_source(
        tmp_path, "live.proto", _collision_proto("live_only", "MOOD_LIVE")
    )
    explicit = tmp_path / "explicit.proto"
    explicit.write_text(
        _collision_proto("explicit_only", "MOOD_EXPLICIT"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        completion_engine,
        "_reflection_source",
        lambda *_: completion_engine._OpenedReflectionSource(
            reflected,
            nullcontext(),  # type: ignore[arg-type]
        ),
    )
    common = [
        "--proto",
        str(explicit),
        "--use-reflection",
        "--method",
        "collision.API/Call",
    ]
    fields = complete(
        [*common, "--field", ""],
        environment=environment,
        cwd=tmp_path,
    )
    assert "explicitOnly=" in _values(fields)
    assert "liveOnly=" not in _values(fields)
    assert "savedOnly=" not in _values(fields)
    enums = complete(
        [*common, "--field", "mood="],
        environment=environment,
        cwd=tmp_path,
    )
    assert "mood=MOOD_EXPLICIT" in _values(enums)
    assert "mood=MOOD_LIVE" not in _values(enums)
    assert "mood=MOOD_SAVED" not in _values(enums)
    output_fields = complete(
        [*common, "--output-field", ""],
        environment=environment,
        cwd=tmp_path,
    )
    assert "explicitOnlyReply" in _values(output_fields)
    assert "liveOnlyReply" not in _values(output_fields)
    assert "savedOnlyReply" not in _values(output_fields)
    methods = complete(
        [
            "--proto",
            str(explicit),
            "--use-reflection",
            "--method",
            "",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    assert "collision.API/Call" in _values(methods)
    assert "collision.Legacy/Old" in _values(methods)
    assert _values(methods).count("collision.API/Call") == 1


def test_saved_scan_preserves_nested_and_oneof_symbol_scopes(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    saved = tmp_path / "data" / "protodial" / "protos" / "formats.proto"
    saved.parent.mkdir(parents=True)
    repository_proto = (
        Path(__file__).parents[1] / "examples" / "formats" / "formats.proto"
    )
    saved.write_bytes(repository_proto.read_bytes())
    sources = completion_engine._local_sources(
        completion_engine._scan([""]), environment
    )
    values = _values(completion_engine._symbols(sources))
    assert "examples.formats.Document.Priority" in values
    assert "examples.formats.Document.PRIORITY_HIGH" in values
    assert "examples.formats.Priority" not in values
    assert "examples.formats.Document.email" in values
    assert "examples.formats.Document.queue_id" in values
    assert "examples.formats.Document.labels" in values


def test_saved_proto_parser_ignores_comments_strings_and_nested_services(
    tmp_path: Path,
) -> None:
    path = tmp_path / "structural.proto"
    path.write_text(
        'syntax = "proto3"; package structural;\n'
        "// service CommentGhost { rpc Fake(X) returns (Y); }\n"
        "message Outer {\n"
        '  option (custom.note) = "message StringGhost {}";\n'
        "  message Inner { string linked = 1; }\n"
        "  enum State { STATE_UNSPECIFIED = 0; STATE_READY = 1; }\n"
        "  oneof destination { string email = 2; uint64 queue_id = 3; }\n"
        "  service InvalidNested { rpc Invented(X) returns (Y); }\n"
        "}\n"
        "service API { rpc Call(Outer)returns(Outer); }\n",
        encoding="utf-8",
    )
    entry = parse_proto(str(path), ScanBudget())
    assert entry is not None
    values = _values(list(entry.symbols))
    assert "structural.API/Call" in _values(list(entry.methods))
    assert "structural.Outer.Inner.linked" in values
    assert "structural.Outer.STATE_READY" in values
    assert "structural.Outer.email" in values
    assert not any("Ghost" in value for value in values)
    assert not any("InvalidNested" in value for value in values)


def test_saved_proto_scan_is_bounded_and_skips_unsafe_entries(
    tmp_path: Path,
) -> None:
    environment, regular = _saved_proto(tmp_path)
    root = regular.parent
    (root / ".hidden.proto").write_text(PROTO, encoding="utf-8")
    _symlink_or_skip(root / "linked.proto", regular)
    _mkfifo_or_skip(root / "blocked.proto")
    huge = root / "huge.proto"
    with huge.open("wb") as output:
        output.seek(1024 * 1024 + 1)
        output.write(b"x")
    for index in range(540):
        (root / f"many-{index:03}.proto").touch()

    started = time.monotonic()
    paths = saved_proto_files(environment)
    assert time.monotonic() - started < 1.0
    assert len(paths) <= 512
    assert str(root / ".hidden.proto") not in paths
    assert str(root / "linked.proto") not in paths
    assert str(root / "blocked.proto") not in paths

    entry = parse_proto(str(huge), ScanBudget())
    assert entry is None


def test_saved_lexical_read_rejects_file_swapped_to_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "schema.proto"
    source.write_text(PROTO, encoding="utf-8")
    original_open = saved_protos_module.os.open
    swapped = False

    def swap_then_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.rename(tmp_path / "original.proto")
            _mkfifo_or_skip(source)
        return original_open(  # type: ignore[call-overload]
            path, flags, *args, **kwargs
        )

    monkeypatch.setattr(saved_protos_module.os, "open", swap_then_open)
    started = time.monotonic()
    assert parse_proto(str(source), ScanBudget()) is None
    assert swapped
    assert time.monotonic() - started < 0.5


def test_saved_method_compiles_on_demand_for_field_and_enum_completion(
    tmp_path: Path,
) -> None:
    environment, _ = _saved_proto(tmp_path)
    common = ["--method", "saved.example.Greeter/SayHello"]
    fields = complete(
        [*common, "--field", "display"],
        environment=environment,
        cwd=tmp_path,
    )
    enums = complete(
        [*common, "--field", "mood="],
        environment=environment,
        cwd=tmp_path,
    )
    assert "displayName=" in _values(fields)
    assert "mood=MOOD_HAPPY" in _values(enums)


def test_saved_fifo_import_is_bounded_by_compiler_timeout(
    tmp_path: Path,
) -> None:
    environment, proto = _saved_proto(tmp_path)
    proto.write_text(
        'syntax = "proto3"; package saved.example; '
        'import "blocked.proto"; '
        "service Greeter { rpc SayHello(Request) returns (Reply); }",
        encoding="utf-8",
    )
    _mkfifo_or_skip(proto.parent / "blocked.proto")
    started = time.monotonic()
    fields = complete(
        [
            "--method",
            "saved.example.Greeter/SayHello",
            "--field",
            "",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    assert fields == []
    assert time.monotonic() - started < 1.75


def test_explicit_proto_with_more_than_128_imports_still_completes(
    tmp_path: Path,
) -> None:
    imports: list[str] = []
    for index in range(129):
        name = f"dependency-{index}.proto"
        imports.append(f'import "{name}";')
        (tmp_path / name).write_text(
            f'syntax = "proto3"; package broad; message Dependency{index} {{}}',
            encoding="utf-8",
        )
    entrypoint = tmp_path / "api.proto"
    entrypoint.write_text(
        'syntax = "proto3"; package broad; '
        + " ".join(imports)
        + " service API { rpc Call(Request) returns (Reply); }"
        + " message Request { string name = 1; } message Reply {}",
        encoding="utf-8",
    )
    fields = complete(
        [
            "--proto",
            str(entrypoint),
            "--method",
            "broad.API/Call",
            "--field",
            "na",
        ],
        environment=_environment(tmp_path),
        cwd=tmp_path,
    )
    assert "name=" in _values(fields)


@pytest.mark.parametrize("through_symlink", [False, True])
def test_protoset_completion_uses_a_private_snapshot(
    tmp_path: Path, through_symlink: bool
) -> None:
    original = _write_protoset(tmp_path)
    supplied = original
    if through_symlink:
        supplied = tmp_path / "linked.protoset"
        _symlink_or_skip(supplied, original)
    scan = completion_engine._scan(
        [
            "--protoset",
            str(supplied),
            "--method",
            "saved.example.Greeter/SayHello",
            "--field",
            "",
        ]
    )
    sources = completion_engine._local_sources(scan, _environment(tmp_path))
    assert sources
    source = sources[0]
    owner = source._protodial_completion_snapshot  # type: ignore[attr-defined]
    snapshot_directory = Path(owner.name)
    assert snapshot_directory.is_dir()
    original.unlink()
    assert source.find_symbol("saved.example.Greeter/SayHello") is not None
    assert "displayName=" in _values(
        completion_engine._field_candidates(scan, "field", "", sources)
    )

    del sources, source, owner
    gc.collect()
    assert not snapshot_directory.exists()


def test_protoset_snapshot_rejects_file_swapped_to_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_protoset(tmp_path)
    original_open = saved_protos_module.os.open
    swapped = False

    def swap_then_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.rename(tmp_path / "original.protoset")
            _mkfifo_or_skip(source)
        return original_open(  # type: ignore[call-overload]
            path, flags, *args, **kwargs
        )

    monkeypatch.setattr(saved_protos_module.os, "open", swap_then_open)
    started = time.monotonic()
    sources = completion_engine._local_sources(
        completion_engine._scan(["--protoset", str(source), ""]),
        _environment(tmp_path),
    )
    assert sources == []
    assert swapped
    assert time.monotonic() - started < 0.5


def test_protoset_snapshot_rejects_oversized_input(tmp_path: Path) -> None:
    source = tmp_path / "large.protoset"
    with source.open("wb") as output:
        output.seek(8 * 1024 * 1024)
        output.write(b"x")
    started = time.monotonic()
    sources = completion_engine._local_sources(
        completion_engine._scan(["--protoset", str(source), ""]),
        _environment(tmp_path),
    )
    assert sources == []
    assert time.monotonic() - started < 0.5


def test_saved_lazy_compile_uses_runtime_proto_root_precedence(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    saved = tmp_path / "data" / "protodial" / "protos"
    custom = tmp_path / "custom"
    saved.mkdir(parents=True)
    custom.mkdir()
    (saved / "api.proto").write_text(
        'syntax = "proto3"; package roots; import "common.proto"; '
        "service API { rpc Call(Request) returns (Reply); }",
        encoding="utf-8",
    )
    (saved / "common.proto").write_text(
        'syntax = "proto3"; package roots; '
        "message Request { string default_name = 1; } message Reply {}",
        encoding="utf-8",
    )
    (custom / "common.proto").write_text(
        'syntax = "proto3"; package roots; '
        "message Request { string custom_name = 1; } message Reply {}",
        encoding="utf-8",
    )
    fields = complete(
        [
            "--proto-path",
            str(custom),
            "--method",
            "roots.API/Call",
            "--field",
            "",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    assert "defaultName=" in _values(fields)
    assert "customName=" not in _values(fields)


def test_fresh_saved_proto_candidates_obey_prefix(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    root = tmp_path / "data" / "protodial" / "protos"
    root.mkdir(parents=True)
    alpha = root / "alpha.proto"
    beta = root / "beta.proto"
    alpha.touch()
    beta.touch()
    values = set(
        _values(
            complete(
                ["--proto", str(root / "alp")],
                environment=environment,
                cwd=tmp_path,
            )
        )
    )
    assert str(alpha) in values
    assert str(beta) not in values


def test_proto_filename_completion_is_not_truncated_at_256(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    for index in range(300):
        (tmp_path / f"schema-{index:03}.proto").touch()
    values = _values(
        complete(
            ["--proto", "schema-299"],
            environment=environment,
            cwd=tmp_path,
        )
    )
    assert "schema-299.proto" in values


def test_path_and_recursive_proto_scans_have_wall_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(100):
        (tmp_path / f"schema-{index:03}.proto").touch()
    original_scandir = os.scandir

    class SlowScandir:
        def __init__(self, path: object) -> None:
            self.entries = original_scandir(path)  # type: ignore[arg-type]

        def __enter__(self) -> SlowScandir:
            return self

        def __exit__(self, *_args: object) -> None:
            self.entries.close()

        def __iter__(self) -> SlowScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            time.sleep(0.01)
            return next(self.entries)

    monkeypatch.setattr(completion_engine.os, "scandir", SlowScandir)
    started = time.monotonic()
    direct = completion_engine._path_candidates("schema", tmp_path)
    walked = list(completion_engine._walk_files(tmp_path, ".proto"))
    elapsed = time.monotonic() - started
    assert elapsed < 0.75
    assert len(direct) < 100
    assert len(walked) < 100


def test_continuation_candidates_suppress_trailing_space(
    tmp_path: Path,
) -> None:
    environment, proto = _saved_proto(tmp_path)
    field = complete(
        [
            "--proto",
            str(proto),
            "--method",
            "saved.example.Greeter/SayHello",
            "--field",
            "mo",
        ],
        environment=environment,
        cwd=tmp_path,
    )
    mood = next(item for item in field if item.value == "mood=")
    assert mood.append_space is False
    directory = tmp_path / "nested"
    directory.mkdir()
    paths = complete(["--proto", "ne"], environment=environment, cwd=tmp_path)
    nested = next(item for item in paths if item.value == "nested/")
    assert nested.append_space is False


def test_completion_scripts_use_nul_protocol_without_eval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_completion_command(["bash"]) == 0
    bash = capsys.readouterr().out
    assert "read -r -d ''" in bash
    assert "__complete" in bash
    assert "compopt" not in bash
    assert "complete -o nospace" in bash
    assert "eval" not in bash

    assert run_completion_command(["zsh"]) == 0
    zsh = capsys.readouterr().out
    assert "read -r -d ''" in zsh
    assert "compadd" in zsh
    assert "eval" not in zsh


def test_private_protocol_is_nul_delimited(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        completion_engine,
        "complete",
        lambda *_args, **_kwargs: [Candidate("a b", "two words")],
    )
    assert (
        run_completion_protocol(
            ["--protocol=protodial-v1", "--shell", "bash", "--", ""]
        )
        == 0
    )
    assert capfd.readouterr().out == "a b\0two words\0" + "1\0"


def test_private_protocol_forwards_cursor_index(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_complete(*_args: object, **kwargs: object) -> list[Candidate]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(completion_engine, "complete", fake_complete)
    monkeypatch.chdir(tmp_path)
    assert (
        run_completion_protocol(
            [
                "--protocol=protodial-v1",
                "--shell",
                "bash",
                "--cursor",
                "1",
                "--",
                "one",
                "two",
            ]
        )
        == 0
    )
    capfd.readouterr()
    assert observed["cursor"] == 1


def test_private_protocol_accepts_nul_words_on_stdin() -> None:
    root = Path(__file__).parents[1]
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "protodial",
            "__complete",
            "--protocol=protodial-v1",
            "--shell",
            "bash",
            "--cursor",
            "1",
            "--words-stdin",
        ],
        input=b"--compression\0g\0--color\0never\0",
        capture_output=True,
        env=environment,
        check=True,
    )
    assert completed.stdout.split(b"\0") == [
        b"gzip",
        b"compression value",
        b"1",
        b"",
    ]


def test_native_target_named_private_command_uses_ordinary_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: object, *_args: object) -> int:
        observed["command"] = command
        return 0

    monkeypatch.setattr(cli_module, "_run", fake_run)
    assert main(["__complete", "--method", "example.API/Call"]) == 0
    command = observed["command"]
    assert command.target.channel == "__complete"  # type: ignore[attr-defined]
    assert command.operation.method == "example.API/Call"  # type: ignore[attr-defined]


def test_native_target_named_completion_uses_ordinary_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: object, *_args: object) -> int:
        observed["command"] = command
        return 0

    monkeypatch.setattr(cli_module, "_run", fake_run)
    assert main(["completion", "--method", "example.API/Call"]) == 0
    command = observed["command"]
    assert command.target.channel == "completion"  # type: ignore[attr-defined]
    assert command.operation.method == "example.API/Call"  # type: ignore[attr-defined]


def test_public_completion_management_grammar_still_dispatches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["completion", "bash"]) == 0
    assert "_protodial_complete" in capsys.readouterr().out


def _fake_protodial(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "protodial"
    log = tmp_path / "arguments.log"
    input_log = tmp_path / "input.log"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$PROTODIAL_TEST_LOG"\n'
        'cat > "$PROTODIAL_TEST_INPUT"\n'
        'if [ "$PROTODIAL_TEST_MODE" = array ]; then\n'
        "  printf 'moods=[\"MOOD_HAPPY\"]\\000enum array\\0001\\000'\n"
        'elif [ "$PROTODIAL_TEST_MODE" = value ]; then\n'
        "  printf 'mood=MOOD_HAPPY\\000enum\\0001\\000'\n"
        "else\n"
        "  printf 'mood=\\000enum\\0000\\000'\n"
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log, input_log


def _read_pty(file_descriptor: int, duration: float = 0.2) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        readable, _, _ = select.select([file_descriptor], [], [], 0.03)
        if not readable:
            continue
        try:
            output.extend(os.read(file_descriptor, 65536))
        except OSError:
            break
    return bytes(output)


def _readline_completion(
    tmp_path: Path, line: str, candidate: str
) -> list[str]:
    if pty is None or not Path("/bin/bash").is_file():
        pytest.skip("interactive Bash completion requires a Unix PTY")
    executable = tmp_path / "protodial"
    log = tmp_path / "readline-arguments.json"
    ready = tmp_path / "readline-completion-ready"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "if sys.argv[1:2] == ['__complete']:\n"
        "    sys.stdin.buffer.read()\n"
        "    value = os.environ['PROTODIAL_PTY_VALUE'].encode()\n"
        "    sys.stdout.buffer.write(value + b'\\0value\\0' + b'1\\0')\n"
        "    sys.stdout.buffer.flush()\n"
        "    pathlib.Path(os.environ['PROTODIAL_PTY_READY']).touch()\n"
        "else:\n"
        "    pathlib.Path(os.environ['PROTODIAL_PTY_LOG']).write_text(\n"
        "        json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    script = (
        Path(__file__).parents[1]
        / "src"
        / "protodial"
        / "completion"
        / "protodial.bash"
    )
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROTODIAL_PTY_LOG": str(log),
        "PROTODIAL_PTY_VALUE": candidate,
        "PROTODIAL_PTY_READY": str(ready),
    }
    file_descriptor, slave = pty.openpty()
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    try:
        _read_pty(file_descriptor, 0.5)
        setup = f"PS1='READY> '; source {shlex.quote(str(script))}\n"
        os.write(file_descriptor, setup.encode())
        prompt_output = bytearray()
        prompt_deadline = time.monotonic() + 5.0
        while (
            b"READY> " not in prompt_output
            and time.monotonic() < prompt_deadline
        ):
            prompt_output.extend(_read_pty(file_descriptor, 0.05))
        assert b"READY> " in prompt_output
        os.write(file_descriptor, line.encode() + b"\t")
        completion_deadline = time.monotonic() + 5.0
        while not ready.exists() and time.monotonic() < completion_deadline:
            _read_pty(file_descriptor, 0.05)
        assert ready.exists()
        _read_pty(file_descriptor, 0.3)
        os.write(file_descriptor, b"\n")
        deadline = time.monotonic() + 3.0
        while not log.exists() and time.monotonic() < deadline:
            _read_pty(file_descriptor, 0.05)
        assert log.exists(), _read_pty(file_descriptor, 0.2).decode(
            errors="replace"
        )
        return json.loads(log.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    finally:
        try:
            os.write(file_descriptor, b"\x03exit\n")
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
        os.close(file_descriptor)


@pytest.mark.parametrize(
    ("line", "candidate", "expected"),
    [
        (
            "protodial --compression=g",
            "--compression=gzip",
            ["--compression=gzip"],
        ),
        (
            "protodial grpc://example.test/pkg.",
            "grpc://example.test/pkg.Service/Call",
            ["grpc://example.test/pkg.Service/Call"],
        ),
        (
            "protodial --field moods=",
            'moods=["MOOD_HAPPY"]',
            ["--field", 'moods=["MOOD_HAPPY"]'],
        ),
        ("protodial some", "some file.proto", ["some file.proto"]),
        (
            "protodial odd",
            "odd'\"\\name.proto",
            ["odd'\"\\name.proto"],
        ),
        (
            'protodial --field "moods=',
            'moods=["MOOD_HAPPY"]',
            ["--field", 'moods=["MOOD_HAPPY"]'],
        ),
        (
            "protodial --field 'moods=",
            'moods=["MOOD_HAPPY"]',
            ["--field", 'moods=["MOOD_HAPPY"]'],
        ),
        (
            'protodial "some',
            'some file"quote.proto',
            ['some file"quote.proto'],
        ),
        (
            'protodial "back',
            "back\\slash $file.proto",
            ["back\\slash $file.proto"],
        ),
        (
            'protodial "wow',
            "wow!literal.proto",
            ["wow!literal.proto"],
        ),
        (
            "protodial $'raw",
            "raw\\n\\t'quote.proto",
            ["raw\\n\\t'quote.proto"],
        ),
        (
            "protodial 'some",
            "some file'quote.proto",
            ["some file'quote.proto"],
        ),
    ],
)
def test_bash_readline_inserts_literal_completion_words(
    tmp_path: Path, line: str, candidate: str, expected: list[str]
) -> None:
    assert _readline_completion(tmp_path, line, candidate) == expected


def test_bash_readline_completion_cannot_execute_command_substitution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "injected"
    candidate = f"evil$(touch {marker}).proto"
    assert _readline_completion(tmp_path, "protodial evil", candidate) == [
        candidate
    ]
    assert not marker.exists()


def test_bash_open_quote_completion_cannot_execute_command_substitution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "quoted-injected"
    candidate = f"evil$(touch {marker}).proto"
    assert _readline_completion(tmp_path, 'protodial "evil', candidate) == [
        candidate
    ]
    assert not marker.exists()


def test_bash_ansi_quote_completion_cannot_execute_substitution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ansi-injected"
    candidate = f"raw\\n$(touch {marker}).proto"
    assert _readline_completion(tmp_path, "protodial $'raw", candidate) == [
        candidate
    ]
    assert not marker.exists()


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_adapter_honors_cursor_and_nospace(
    tmp_path: Path, shell: str
) -> None:
    if os.name == "nt":
        pytest.skip("Bash and Zsh adapters require a Unix-like shell")
    shell_executable = shutil.which(shell)
    if shell_executable is None:
        pytest.skip(f"{shell} is unavailable")
    _executable, log, input_log = _fake_protodial(tmp_path)
    root = Path(__file__).parents[1]
    script = (
        root / "src" / "protodial" / "completion" / "protodial.bash"
        if shell == "bash"
        else root / "src" / "protodial" / "completion" / "_protodial"
    )
    if shell == "bash":
        program = """
source "$1"
COMP_WORDS=(protodial --field '' --key-password supersecret \
    grpc://later.example:443)
COMP_CWORD=2
_protodial_complete
printf 'VALUE=<%s>\n' "${COMPREPLY[0]}"
export PROTODIAL_TEST_MODE=value
COMP_WORDS=(protodial --field 'mood=' --key-password supersecret \
    grpc://later.example:443)
COMP_CWORD=2
_protodial_complete
printf 'ENUM=<%s>\n' "${COMPREPLY[0]}"
export PROTODIAL_TEST_MODE=array
COMP_WORDS=(protodial --field 'moods=' grpc://later.example:443)
COMP_CWORD=2
_protodial_complete
printf 'ARRAY=<%s>\n' "${COMPREPLY[0]}"
"""
    else:
        program = """
source "$1"
compadd() { for value in "$@"; do print -r -- "ARG=<$value>"; done; }
words=(protodial --field '' --key-password supersecret \
    grpc://later.example:443)
CURRENT=3
_protodial
"""
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROTODIAL_TEST_LOG": str(log),
        "PROTODIAL_TEST_INPUT": str(input_log),
    }
    completed = subprocess.run(
        [shell_executable, "-c", program, "completion-test", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=True,
    )
    if shell == "bash":
        assert "VALUE=<mood=>" in completed.stdout
        assert "ENUM=<MOOD_HAPPY >" in completed.stdout
        assert 'ARRAY=<\\[\\"MOOD_HAPPY\\"\\] >' in completed.stdout
    else:
        assert "ARG=<-S>" in completed.stdout
        assert "ARG=<mood=>" in completed.stdout
    arguments = log.read_text(encoding="utf-8").splitlines()
    assert arguments[:5] == [
        "__complete",
        "--protocol=protodial-v1",
        "--shell",
        shell,
        "--cursor",
    ]
    assert arguments[5] == "1"
    assert arguments[-1] == "--words-stdin"
    assert "supersecret" not in arguments
    words = input_log.read_bytes().split(b"\0")
    if shell == "bash":
        assert words[:-1] == [
            b"--field",
            b"moods=",
            b"grpc://later.example:443",
        ]
    else:
        assert words[:-1] == [
            b"--field",
            b"",
            b"--key-password",
            b"supersecret",
            b"grpc://later.example:443",
        ]


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("word_style", ["plain", "quoted", "escaped"])
def test_shell_adapter_truncates_the_active_word_at_cursor(
    tmp_path: Path, shell: str, word_style: str
) -> None:
    if os.name == "nt":
        pytest.skip("Bash and Zsh adapters require a Unix-like shell")
    shell_executable = shutil.which(shell)
    if shell_executable is None:
        pytest.skip(f"{shell} is unavailable")
    root = Path(__file__).parents[1]
    script = (
        root / "src" / "protodial" / "completion" / "protodial.bash"
        if shell == "bash"
        else root / "src" / "protodial" / "completion" / "_protodial"
    )
    log = tmp_path / "cursor-words.bin"
    executable = tmp_path / "protodial"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, subprocess, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "pathlib.Path(os.environ['PROTODIAL_CURSOR_LOG']).write_bytes(data)\n"
        "result = subprocess.run(\n"
        "    [sys.executable, '-m', 'protodial', *sys.argv[1:]],\n"
        "    input=data, stdout=subprocess.PIPE, check=False)\n"
        "sys.stdout.buffer.write(result.stdout)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    if word_style == "quoted":
        line = 'protodial "--compression=gBAD extra" --header "later value"'
        left = 'protodial "--compression=g'
        words = [
            "protodial",
            "--compression=gBAD extra",
            "--header",
            "later value",
        ]
        active = 1
    elif word_style == "escaped":
        line = "protodial --compression=\\gBAD --header later\\ value"
        left = "protodial --compression=\\g"
        words = [
            "protodial",
            "--compression",
            "=",
            "gBAD",
            "--header",
            "later value",
        ]
        active = 3
    else:
        line = 'protodial --compression=gBAD --header "later value"'
        left = "protodial --compression=g"
        words = [
            "protodial",
            "--compression",
            "=",
            "gBAD",
            "--header",
            "later value",
        ]
        active = 3
    shell_words = " ".join(shlex.quote(word) for word in words)
    if shell == "bash":
        program = f"""
source "$1"
COMP_LINE={shlex.quote(line)}
COMP_POINT={len(left)}
COMP_WORDS=({shell_words})
COMP_CWORD={active}
_protodial_complete
printf 'VALUE=<%s>\n' "${{COMPREPLY[0]}}"
"""
    else:
        program = f"""
source "$1"
compadd() {{ for value in "$@"; do print -r -- "ARG=<$value>"; done; }}
words=({shell_words})
CURRENT={active + 1}
PREFIX='--compression=g'
SUFFIX='BAD'
_protodial
"""
    completed = subprocess.run(
        [shell_executable, "-c", program, "cursor-test", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(root / "src"),
            "PROTODIAL_CURSOR_LOG": str(log),
        },
        check=True,
    )
    if shell == "bash":
        assert "gzip" in completed.stdout
    else:
        assert "ARG=<--compression=gzip>" in completed.stdout
    supplied = log.read_bytes().split(b"\0")[:-1]
    assert b"--compression=g" in supplied
    assert supplied[-2:] == [b"--header", b"later value"]


def test_read_only_entrypoints_work_with_unwritable_data_home(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/dev/null")
    assert main(["--version"]) == 0
    assert capsys.readouterr().out
    assert main(["completion", "bash"]) == 0
    assert "_protodial_complete" in capsys.readouterr().out


def test_completion_install_uses_xdg_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert main(["completion", "install"]) == 0
    installed = tmp_path / "bash-completion" / "completions" / "protodial"
    captured = capsys.readouterr()
    assert Path(captured.out.strip()) == installed
    assert f"source {shlex.quote(str(installed))}" in captured.err
    assert installed.read_text(encoding="utf-8").startswith("# bash completion")
    assert main(["completion", "install"]) == 2
    assert "use --force" in capsys.readouterr().err
    assert main(["completion", "install", "--force"]) == 0


def test_completion_install_supports_explicit_zsh_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "custom" / "_protodial"
    assert (
        main(
            [
                "completion",
                "install",
                "--shell",
                "zsh",
                "--path",
                str(path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert path.read_text(encoding="utf-8").startswith("#compdef protodial")
    if os.name != "nt":
        assert stat_mode(path) == 0o644


@pytest.mark.parametrize(
    ("shell", "probe"),
    [
        ("bash", "declare -F _protodial_complete >/dev/null"),
        ("zsh", "(( $+functions[_protodial] ))"),
    ],
)
def test_install_output_is_one_command_sourceable_activation(
    tmp_path: Path, shell: str, probe: str
) -> None:
    if os.name == "nt":
        pytest.skip("Bash and Zsh activation requires a Unix-like shell")
    shell_executable = shutil.which(shell)
    if shell_executable is None:
        pytest.skip(f"{shell} is unavailable")
    root = Path(__file__).parents[1]
    python = root / ".venv" / "bin" / "python"
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / shell),
        "PYTHONPATH": str(root / "src"),
        "SHELL": f"/bin/{shell}",
    }
    program = f'source "$("$1" -m protodial completion install)"; {probe}'
    subprocess.run(
        [shell_executable, "-c", program, "activation-test", str(python)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=True,
    )


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
