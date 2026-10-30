from __future__ import annotations

import time
from pathlib import Path

import pytest

import protodial.completion._saved_protos as saved_protos_module
import protodial.completion.engine as completion_engine
from protodial.completion._saved_protos import (
    ScanBudget,
    parse_proto,
    saved_proto_files,
)
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


@pytest.mark.parametrize(
    ("declaration", "nested_symbols"),
    [
        (
            "message Inner { optional string own = 1; } "
            "optional Inner after = 2;",
            {"Inner", "Inner.own"},
        ),
        (
            "enum Kind { ZERO = 0; } optional Kind after = 2;",
            {"Kind", "ZERO"},
        ),
        (
            "oneof pick { string choice = 1; } optional string after = 2;",
            {"pick", "choice"},
        ),
        (
            "extend Base { optional string extension = 100; } "
            "optional string after = 2;",
            {"extension"},
        ),
    ],
)
def test_saved_symbols_include_same_line_fields_after_declaration_blocks(
    tmp_path: Path, declaration: str, nested_symbols: set[str]
) -> None:
    environment, path = _saved_proto(tmp_path)
    content = (
        'syntax = "proto2"; package compact; '
        "message Base { extensions 100 to max; } "
        f"message Outer {{ {declaration} optional string last = 3; }}"
    )
    path.write_text(content, encoding="utf-8")
    source = _compiled_source(tmp_path, "compiled.proto", content)
    expected = {
        f"compact.Outer.{name}" for name in {"after", "last", *nested_symbols}
    }
    for name in expected:
        assert source.find_symbol(name) is not None  # type: ignore[attr-defined]

    candidates = complete(
        ["describe", "unused.invalid:1", "compact.Outer."],
        environment=environment,
        cwd=tmp_path,
    )

    assert set(_values(candidates)) == expected


def test_saved_scan_preserves_nested_and_oneof_symbol_scopes(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    saved = tmp_path / "data" / "protodial" / "protos" / "formats.proto"
    saved.parent.mkdir(parents=True)
    repository_proto = (
        Path(__file__).parents[2] / "examples" / "formats" / "formats.proto"
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


@pytest.mark.parametrize("hexadecimal_numbers", [False, True])
def test_saved_proto2_group_symbols_match_compiled_schema(
    tmp_path: Path, hexadecimal_numbers: bool
) -> None:
    environment, path = _saved_proto(tmp_path)
    content = (
        'syntax = "proto2"; package compact; '
        "message Base { extensions 100 to max; } "
        "message Outer { "
        "optional group MyInfo = 1 [deprecated = true] { "
        "optional string name = 2; "
        "optional group NestedThing = 3 { optional string value = 4; } "
        "optional string after_nested = 5; } "
        "extend Base { optional group ExtraInfo = 100 { "
        "optional string extra = 1; } optional string after_extra = 102; } "
        "optional string after_group = 6; } "
        "extend Base { optional group TopGroup = 101 { "
        "optional string top = 1; } }"
    )
    if hexadecimal_numbers:
        for number in (100, 101, 102, 1, 2, 3, 4, 5, 6):
            content = content.replace(f"= {number};", f"= {number:#x};")
            content = content.replace(f"= {number} ", f"= {number:#x} ")
    path.write_text(content, encoding="utf-8")
    source = _compiled_source(tmp_path, "compiled.proto", content)
    compiled = completion_engine._symbols([source])  # type: ignore[list-item]
    candidates = complete(
        ["describe", "unused.invalid:1", "compact."],
        environment=environment,
        cwd=tmp_path,
    )

    assert set(_values(candidates)) == set(_values(compiled))
    assert {
        "compact.Outer.myinfo",
        "compact.Outer.MyInfo",
        "compact.Outer.MyInfo.name",
        "compact.Outer.MyInfo.nestedthing",
        "compact.Outer.MyInfo.NestedThing.value",
        "compact.Outer.extrainfo",
        "compact.Outer.ExtraInfo.extra",
        "compact.topgroup",
        "compact.TopGroup.top",
    } <= set(_values(candidates))
