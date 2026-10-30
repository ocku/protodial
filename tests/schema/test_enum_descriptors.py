from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool

from protodial.cli import main
from protodial.presentation.descriptor import render_descriptor


@pytest.mark.parametrize("deprecated", [None, False, True])
def test_enum_value_descriptor_matches_its_defining_enum(
    deprecated: bool | None,
) -> None:
    proto = descriptor_pb2.FileDescriptorProto(
        name="value.proto", package="example", syntax="proto3"
    )
    enum = proto.enum_type.add(name="Priority")
    value = enum.value.add(name="LOW", number=0)
    if deprecated is not None:
        value.options.deprecated = deprecated
    file = descriptor_pool.DescriptorPool().Add(proto)
    enum_descriptor = file.enum_types_by_name["Priority"]
    suffix = (
        f" [deprecated = {str(deprecated).lower()}]"
        if deprecated is not None
        else ""
    )
    rendered = f"LOW = 0{suffix};"

    assert render_descriptor(enum_descriptor.values[0]) == rendered
    assert render_descriptor(enum_descriptor) == (
        f"enum example.Priority {{\n  {rendered}\n}}"
    )


def test_describe_enum_value_from_proto_is_accessible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proto = tmp_path / "value.proto"
    proto.write_text(
        'syntax = "proto3";\npackage example;\n'
        "message Document {\n  enum Priority {\n"
        "    // Prefer low priority.\n"
        "    LOW = 0 [deprecated = true];\n  }\n}\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "describe",
                "--proto",
                str(proto),
                "unused.example:443",
                "example.Document.LOW",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == (
        "// Prefer low priority.\nLOW = 0 [deprecated = true];\n"
    )
    assert captured.err == ""
