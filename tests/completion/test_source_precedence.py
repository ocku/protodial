from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

import protodial.completion.engine as completion_engine
from protodial.completion.engine import complete

from .support import (
    _collision_proto,
    _compiled_source,
    _environment,
    _saved_proto,
    _values,
)

pytestmark = pytest.mark.usefixtures("schema_compiler_budget")


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
