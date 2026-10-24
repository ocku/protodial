import io
import logging
import os
import runpy
import subprocess
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2
from grpc_tools import protoc

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"


@pytest.mark.parametrize(
    ("name", "service", "port"),
    [
        ("hello", "examples.hello.Greeter", 50051),
        ("streaming", "examples.streaming.Streamer", 50052),
        ("errors", "examples.errors.Validator", 50053),
        ("formats", "examples.formats.Codec", 50054),
        ("deadlines", "examples.deadlines.Sleeper", 50055),
    ],
)
def test_example_is_complete_and_proto_compiles(
    tmp_path: Path,
    name: str,
    service: str,
    port: int,
) -> None:
    directory = EXAMPLES / name
    proto = directory / f"{name}.proto"
    descriptor_set = tmp_path / f"{name}.pb"

    for required in (proto, directory / "server.py", directory / "Dockerfile"):
        assert required.is_file()
    assert (directory / "call.sh").is_file()
    server = (directory / "server.py").read_text()
    dockerfile = (directory / "Dockerfile").read_text()
    assert "configure_logging" in server
    assert "log_event" in server
    assert "COPY examples/server_logging.py ./" in dockerfile

    status = protoc.main(
        [
            "protodial-example-test",
            f"--proto_path={directory}",
            f"--descriptor_set_out={descriptor_set}",
            "--include_imports",
            proto.name,
        ]
    )

    assert status == 0
    files = descriptor_pb2.FileDescriptorSet.FromString(
        descriptor_set.read_bytes()
    ).file
    services = {
        f"{file.package}.{declared.name}"
        for file in files
        for declared in file.service
    }
    assert service in services
    assert f"EXPOSE {port}" in dockerfile


def test_example_logging_is_structured_and_configurable() -> None:
    logging_helper = (EXAMPLES / "server_logging.py").read_text()

    assert "PROTODIAL_EXAMPLE_LOG_LEVEL" in logging_helper
    assert 'message = f"event={safe_event}"' in logging_helper
    assert "json.dumps" in logging_helper


def test_example_logging_escapes_line_bidi_and_terminal_controls() -> None:
    namespace = runpy.run_path(str(EXAMPLES / "server_logging.py"))
    log_event = namespace["log_event"]
    stream = io.StringIO()
    logger = logging.getLogger("protodial.test.secure-example-log")
    logger.handlers = [logging.StreamHandler(stream)]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "rpc\u2028forged\u202etest\x1b[2J",
        value="line\nnext\u2066spoof",
    )

    rendered = stream.getvalue()
    assert len(rendered.splitlines()) == 1
    assert "\u2028" not in rendered
    assert "\u202e" not in rendered
    assert "\u2066" not in rendered
    assert "\x1b" not in rendered
    assert r"\u2028" in rendered
    assert r"\u202e" in rendered
    assert r"\u2066" in rendered
    assert r"\u001b" in rendered


def test_readme_response_json_uses_actual_compact_spacing() -> None:
    readme = (ROOT / "README.md").read_text()

    assert '{"message":"Hello, Ada!","requestId":"example-123"}' in readme
    assert '{"message": "Hello, Ada!"' not in readme


def test_call_heading_is_not_disguised_as_a_log_record() -> None:
    result = subprocess.run(
        [
            "sh",
            "-c",
            '. "$EXAMPLES_DIR/lib.sh"; heading "$1"',
            "example-heading",
            "Structured validation failure",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "EXAMPLES_DIR": str(EXAMPLES)},
    )

    assert result.stdout == "\n== Structured validation failure ==\n"
    assert result.stderr == ""


def test_example_runners_reference_every_server() -> None:
    runner = (EXAMPLES / "run.sh").read_text()
    calls = (EXAMPLES / "call.sh").read_text()

    for name in ("hello", "streaming", "errors", "formats", "deadlines"):
        assert name in runner
        assert name in calls
