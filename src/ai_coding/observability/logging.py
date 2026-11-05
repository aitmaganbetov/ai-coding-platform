"""Structured logging (structlog) with secret redaction and run/trace ids (NFR-07)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any, cast

import structlog

from ai_coding.security import redact


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


def redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact every string in the event (keys are kept)."""
    for key in list(event_dict):
        event_dict[key] = _redact_value(event_dict[key])
    return event_dict


class _StreamLoggerFactory:
    """Resolve the output stream at log time, not at configuration time.

    ``sys.stderr`` may be swapped (test runners, daemonisation) after configuration;
    binding the old object would make logging raise on a closed stream, and a
    logging failure must never break a run.
    """

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream

    def __call__(self, *_args: Any) -> structlog.PrintLogger:
        return structlog.PrintLogger(file=self._stream or sys.stderr)


def configure_logging(*, json: bool = True, level: str = "INFO", stream: Any = None) -> None:
    """Configure structlog; JSON lines by default, console renderer for humans."""
    renderer: Any = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=_StreamLoggerFactory(stream),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "ai_coding") -> structlog.typing.FilteringBoundLogger:
    """Named structured logger."""
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(name))


@contextmanager
def bind_run(run_id: str, **extra: Any) -> Iterator[None]:
    """Bind ``run_id`` (and ``trace_id`` = run id by default) to all log lines in scope."""
    tokens = structlog.contextvars.bind_contextvars(
        run_id=run_id, trace_id=extra.pop("trace_id", run_id), **extra
    )
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
