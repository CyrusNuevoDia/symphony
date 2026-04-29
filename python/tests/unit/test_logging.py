from __future__ import annotations

import json

from symphony.logging import configure_logging, get_logger


def test_configure_logging_renders_json(capsys) -> None:
    configure_logging(json=True)
    get_logger("tests.logging.json").info("hello", component="unit-test")

    payload = json.loads(capsys.readouterr().err.strip())

    assert payload["event"] == "hello"
    assert payload["level"] == "info"
    assert payload["logger"] == "tests.logging.json"
    assert "timestamp" in payload


def test_configure_logging_renders_console(capsys) -> None:
    configure_logging(json=False)
    get_logger("tests.logging.console").info("hello")

    output = capsys.readouterr().err.strip()

    assert "hello" in output
    assert "info" in output.lower()
    assert "tests.logging.console" in output
