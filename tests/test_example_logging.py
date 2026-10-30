import io
import logging
import runpy
from pathlib import Path

EXAMPLES = Path(__file__).parents[1] / "examples"


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
