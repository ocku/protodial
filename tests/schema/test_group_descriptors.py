from pathlib import Path
from textwrap import indent

import pytest

from protodial.cli import main

GROUP_PROTO = """syntax = "proto2";
package example;
message Target { extensions 100 to max; }
message Container {
  // An ordinary group.
  optional group Details = 1 { optional string name = 2; }
  extend Target {
    // A nested extension group.
    optional group Extra = 100 { optional string note = 1; }
  }
}
extend Target {
  // A file extension group.
  optional group Global = 101 { optional int32 id = 1; }
}
"""
GROUP = (
    "// An ordinary group.\n"
    "optional group Details = 1 {\n  optional string name = 2;\n}"
)
NESTED_EXTENSION = (
    "extend .example.Target {\n"
    "  // A nested extension group.\n"
    "  optional group Extra = 100 {\n"
    "    optional string note = 1;\n  }\n}"
)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("example.Container.details", GROUP),
        ("example.Container.extra", NESTED_EXTENSION),
        (
            "example.global",
            "extend .example.Target {\n"
            "  // A file extension group.\n"
            "  optional group Global = 101 {\n"
            "    optional int32 id = 1;\n  }\n}",
        ),
        (
            "example.Container",
            "message example.Container {\n"
            + indent(GROUP, "  ")
            + "\n"
            + indent(NESTED_EXTENSION, "  ")
            + "\n}",
        ),
    ],
)
def test_describe_groups_preserves_bodies_comments_and_extension_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    symbol: str,
    expected: str,
) -> None:
    proto = tmp_path / "groups.proto"
    proto.write_text(GROUP_PROTO, encoding="utf-8")

    assert (
        main(["describe", "--proto", str(proto), "unused.example:443", symbol])
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == expected + "\n"
    assert captured.err == ""
