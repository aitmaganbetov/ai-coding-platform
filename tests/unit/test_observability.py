"""Structured logging with redaction and run binding (NFR-07)."""

from __future__ import annotations

import io
import json

import pytest
import structlog

from ai_coding.observability import bind_run, configure_logging, get_logger
from ai_coding.observability.logging import redaction_processor


@pytest.fixture(autouse=True)
def _reset_structlog() -> object:
    yield
    structlog.reset_defaults()


def test_json_logs_are_redacted_and_carry_run_id() -> None:
    stream = io.StringIO()
    configure_logging(json=True, level="DEBUG", stream=stream)
    token = "ghp_" + "q" * 36
    with bind_run("run-1"):
        get_logger().info("calling", url=f"https://x:{token}@github.com", nested={"t": [token]})
    get_logger().info("outside")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["run_id"] == "run-1" and lines[0]["trace_id"] == "run-1"
    assert token not in stream.getvalue()
    assert "run_id" not in lines[1]
    configure_logging(json=False, level="WARNING", stream=io.StringIO())


def test_logging_follows_replaced_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream closed after configuration must not break logging (default: live stderr)."""
    first = io.StringIO()
    monkeypatch.setattr("sys.stderr", first)
    configure_logging(json=True, level="INFO")
    first.close()
    second = io.StringIO()
    monkeypatch.setattr("sys.stderr", second)
    get_logger().info("still works")
    assert "still works" in second.getvalue()


def test_processor_keeps_non_strings() -> None:
    event = redaction_processor(None, "info", {"n": 1, "t": ("password=abcdefgh",)})
    assert event["n"] == 1 and "abcdefgh" not in event["t"][0]
