"""Retry & Recovery: exponential backoff with full jitter and error classification (FR-09)."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from enum import StrEnum

from ai_coding.domain.errors import PermanentError, TransientError


class ErrorClass(StrEnum):
    """How a failure is treated by the retry loop."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


def classify(exc: BaseException) -> ErrorClass:
    """Classify an exception.

    Explicit :class:`PermanentError` (including policy, approval, budget and
    success-criteria failures) is never retried. Timeouts, connection problems and
    explicit :class:`TransientError` are retried. Anything else is treated as
    transient but still bounded by ``max_attempts``.
    """
    if isinstance(exc, PermanentError):
        return ErrorClass.PERMANENT
    if isinstance(exc, TransientError | TimeoutError | ConnectionError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, ValueError | TypeError | KeyError | NotImplementedError):
        return ErrorClass.PERMANENT
    return ErrorClass.TRANSIENT


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff parameters; ``delay(n)`` is the wait before attempt ``n + 1``."""

    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 60.0
    jitter: bool = True

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Backoff after ``attempt`` failed attempts (``attempt >= 1``)."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        ceiling = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1))
        if not self.jitter:
            return ceiling
        return (rng or random.SystemRandom()).uniform(0, ceiling)

    async def sleep(self, attempt: int) -> None:
        """Wait the backoff delay."""
        await asyncio.sleep(self.delay(attempt))
