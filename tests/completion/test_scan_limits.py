from __future__ import annotations

import gc
import os
import time
from pathlib import Path

import pytest

import protodial.completion._saved_protos as saved_protos_module
import protodial.completion.engine as completion_engine
from protodial.completion.engine import complete

from .support import (
    _environment,
    _mkfifo_or_skip,
    _symlink_or_skip,
    _values,
    _write_protoset,
)


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
    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)

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
            nonlocal now
            now += 0.01
            return next(self.entries)

    monkeypatch.setattr(completion_engine.os, "scandir", SlowScandir)
    direct = completion_engine._path_candidates("schema", tmp_path)
    walked = list(completion_engine._walk_files(tmp_path, ".proto"))
    assert 0 < len(direct) < 100
    assert 0 < len(walked) < 100
