from __future__ import annotations

from pathlib import Path

import pytest
from google.protobuf import message_factory

import protodial.completion.engine as completion_engine
from protodial.completion.engine import complete
from protodial.message_io import raw_input as raw_input_module

from .support import _environment, _saved_proto, _values


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
