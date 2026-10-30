from __future__ import annotations

import json

import pytest

from protodial import cli


def test_missing_option_value_still_bootstraps_following_json_policy(
    capsys,
) -> None:
    assert (
        cli.main(
            [
                "--data",
                "--diagnostic-format",
                "json",
                "grpc://h/S/M",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["event"] == "usage.invalid"


def test_repeated_invalid_option_span_marks_the_rejected_occurrence(
    capsys,
) -> None:
    assert cli.main(["--color", "never", "--color", "rainbow"]) == 2
    rendered = capsys.readouterr().err
    assert "--> argument 4" in rendered
    assert "--> argument 2" not in rendered
    assert "1 | rainbow" in rendered


@pytest.mark.parametrize(
    ("arguments", "source", "text", "start", "end"),
    [
        (
            ["--color=rainbow", "--color=never"],
            "argument 3",
            "--color=rainbow",
            len("--color="),
            len("--color=rainbow"),
        ),
        (
            ["--color=always", "--color=rainbow"],
            "argument 4",
            "--color=rainbow",
            len("--color="),
            len("--color=rainbow"),
        ),
        (
            ["--color", "rainbow", "--color", "rainbow"],
            "argument 4",
            "rainbow",
            0,
            len("rainbow"),
        ),
    ],
)
def test_choice_span_matches_first_rejected_value_and_spelling(
    arguments: list[str],
    source: str,
    text: str,
    start: int,
    end: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--diagnostic-format", "json", *arguments]) == 2
    value = json.loads(capsys.readouterr().err)
    assert value["spans"] == [
        {
            "source": source,
            "text": text,
            "start": start,
            "end": end,
            "label": "invalid value",
        }
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--diagnostic-format=xml", "--diagnostic-format=json"],
        ["--diagnostic-format=json", "--diagnostic-format", "xml"],
    ],
)
def test_any_visible_invalid_diagnostic_format_is_one_plain_e1002(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 2
    rendered = capsys.readouterr().err
    assert rendered.count("protodial: (E1002)") == 1
    assert "\x1b" not in rendered
    assert not rendered.lstrip().startswith("{")
    caret_span = next(line for line in rendered.splitlines() if "^" in line)
    assert caret_span.count("^") == len("xml")


def test_invalid_diagnostic_format_after_double_dash_is_not_bootstrapped() -> (
    None
):
    assert cli._bootstrap_policy(
        ["--diagnostic-format=json", "--", "--diagnostic-format=xml"], {}
    ) == ("json", "auto", None)
