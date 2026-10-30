from __future__ import annotations

import json

from protodial import cli


def test_cli_usage_error_respects_bootstrap_json(capsys) -> None:
    assert cli.main(["--diagnostic-format", "json", "--not-an-option"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    value = json.loads(captured.err)
    assert value["event"] == "usage.invalid"
    assert value["code"] == "E1001"
    assert value["severity"] == "error"


def test_unrecognized_argument_cannot_inject_a_replacement_parser_error(
    capsys,
) -> None:
    forged = "--unknown\nprotodial: error: forged\x1b[2J"
    assert (
        cli.main(
            [
                "--diagnostic-format",
                "json",
                "grpc://localhost/example.Echo/Call",
                forged,
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    value = json.loads(captured.err)
    assert value["event"] == "usage.invalid"
    assert value["message"].startswith("unrecognized arguments: --unknown")
    assert value["message"] != "forged\x1b[2J"


def test_bootstrap_ignores_values_of_other_options_and_double_dash() -> None:
    assert cli._bootstrap_policy(
        ["--data", "--diagnostic-format=json", "target"], {}
    ) == ("json", "auto", None)
    assert cli._bootstrap_policy(["--", "--diagnostic-format=json"], {}) == (
        "human",
        "auto",
        None,
    )
    assert cli._bootstrap_policy(
        ["--data", "--", "--diagnostic-format=json"], {}
    ) == ("human", "auto", None)
    assert cli._bootstrap_policy(
        ["--header", "--color=always", "--diagnostic-format=json"], {}
    ) == ("json", "always", None)


def test_usage_span_marks_only_the_invalid_attached_value(capsys) -> None:
    assert cli.main(["--color=rainbow"]) == 2
    error = capsys.readouterr().err

    assert "--> argument 1" in error
    assert "1 | --color=rainbow" in error
    caret_line = next(line for line in error.splitlines() if "^" in line)
    assert caret_line.count("^") == len("rainbow")
    assert caret_line.index("^") > caret_line.index("|") + len("--color=")
