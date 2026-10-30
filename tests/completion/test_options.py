from __future__ import annotations

from pathlib import Path

import pytest

import protodial.completion.engine as completion_engine
from protodial.completion.engine import complete

from .support import PROTO, _environment, _saved_proto, _values

pytestmark = pytest.mark.usefixtures("schema_compiler_budget")


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


def test_option_completion_uses_one_long_form_row_with_short_alias(
    tmp_path: Path,
) -> None:
    candidates = complete(
        ["-"], environment=_environment(tmp_path), cwd=tmp_path
    )
    values = _values(candidates)
    assert "--help" in values
    assert "--insecure" in values
    assert "--data" in values
    assert "--include" in values
    assert "-h" not in values
    assert "-k" not in values
    assert "-d" not in values
    assert "-i" not in values
    data = next(item for item in candidates if item.value == "--data")
    assert "--data" in data.display_text
    assert "-d" in data.display_text
    assert "-- request data or @file" in data.display_text
    assert _values(
        complete(["-d"], environment=_environment(tmp_path), cwd=tmp_path)
    ) == ["--data"]
    assert _values(
        complete(
            ["completion", "-h"],
            environment=_environment(tmp_path),
            cwd=tmp_path,
        )
    ) == ["--help"]


def test_non_option_display_keeps_value_above_its_context(
    tmp_path: Path,
) -> None:
    environment, _ = _saved_proto(tmp_path)
    candidate = next(
        item
        for item in complete(
            ["grpc://unused.invalid:1/"],
            environment=environment,
            cwd=tmp_path,
        )
        if item.value.endswith("saved.example.Greeter/SayHello")
    )
    assert candidate.display_text == (
        "saved.example.Greeter/SayHello\n\u21b3 unary request, unary response"
    )


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
