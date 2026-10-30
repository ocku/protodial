from __future__ import annotations

import os
from pathlib import Path

import pytest

from protodial.completion.engine import complete

from .support import _environment, _saved_proto, _values

SEPARATORS = sorted({os.sep, os.altsep} - {None})


@pytest.mark.parametrize("separator", SEPARATORS)
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize(
    ("option", "marker", "filename"),
    [
        ("--data", "@", "request.json"),
        ("--cacert", "", "ca.pem"),
        ("--proto", "", "api.proto"),
        ("--proto-path", "", None),
    ],
)
def test_directory_prefix_completes_children(
    tmp_path: Path,
    separator: str,
    absolute: bool,
    option: str,
    marker: str,
    filename: str | None,
) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "child").mkdir()
    (directory / "unrelated.txt").touch()
    if filename is not None:
        (directory / filename).touch()
    prefix = str(directory) if absolute else directory.name
    candidates = complete(
        [option, f"{marker}{prefix}{separator}"],
        environment=_environment(tmp_path),
        cwd=tmp_path,
    )

    values = _values(candidates)
    child_value = f"{marker}{Path(prefix) / 'child'}/"
    assert child_value in values
    child = next(
        candidate for candidate in candidates if candidate.value == child_value
    )
    assert not child.append_space
    assert f"{marker}{prefix}/" not in values
    if filename is not None:
        assert f"{marker}{Path(prefix) / filename}" in values
    if option in {"--proto", "--proto-path"}:
        assert f"{marker}{Path(prefix) / 'unrelated.txt'}" not in values


@pytest.mark.parametrize("separator", SEPARATORS)
@pytest.mark.parametrize("absolute", [False, True])
def test_field_file_directory_prefix_completes_children(
    tmp_path: Path, separator: str, absolute: bool
) -> None:
    environment, proto = _saved_proto(tmp_path)
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "payload.bin").touch()
    prefix = str(directory) if absolute else directory.name
    candidates = complete(
        [
            "--proto",
            str(proto),
            "--method",
            "saved.example.Greeter/SayHello",
            "--field:file",
            f"payload={prefix}{separator}",
        ],
        environment=environment,
        cwd=tmp_path,
    )

    assert f"payload={Path(prefix) / 'payload.bin'}" in _values(candidates)


def test_completion_install_path_uses_supplied_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    (working_directory / "completion.bash").touch()
    (tmp_path / "completion.zsh").touch()
    monkeypatch.chdir(tmp_path)

    candidates = complete(
        ["completion", "install", "--path", "completion."],
        environment=_environment(tmp_path),
        cwd=working_directory,
    )

    assert _values(candidates) == ["completion.bash"]
