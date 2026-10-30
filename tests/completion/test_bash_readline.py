from __future__ import annotations

from pathlib import Path

import pytest

from .support import _readline_completion


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
