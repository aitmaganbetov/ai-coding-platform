"""Retry policy and error classification (FR-09)."""

from __future__ import annotations

import random

import pytest

from ai_coding.orchestrator.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    PermanentError,
    PolicyDeniedError,
    SuccessCriteriaError,
    TransientError,
)
from ai_coding.orchestrator.retry import ErrorClass, RetryPolicy, classify


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TransientError("x"), ErrorClass.TRANSIENT),
        (TimeoutError(), ErrorClass.TRANSIENT),
        (ConnectionResetError(), ErrorClass.TRANSIENT),
        (RuntimeError("boom"), ErrorClass.TRANSIENT),
        (PermanentError("x"), ErrorClass.PERMANENT),
        (PolicyDeniedError("x"), ErrorClass.PERMANENT),
        (ApprovalRequiredError("x"), ErrorClass.PERMANENT),
        (BudgetExceededError("x"), ErrorClass.PERMANENT),
        (SuccessCriteriaError("x"), ErrorClass.PERMANENT),
        (ValueError("x"), ErrorClass.PERMANENT),
        (KeyError("x"), ErrorClass.PERMANENT),
    ],
)
def test_classify(exc: BaseException, expected: ErrorClass) -> None:
    assert classify(exc) is expected


def test_exponential_backoff_without_jitter_is_capped() -> None:
    policy = RetryPolicy(base_delay=1, multiplier=2, max_delay=5, jitter=False)
    assert [policy.delay(n) for n in range(1, 6)] == [1, 2, 4, 5, 5]


def test_full_jitter_bounds() -> None:
    policy = RetryPolicy(base_delay=1, multiplier=3, max_delay=100)
    rng = random.Random(42)
    for attempt in range(1, 6):
        ceiling = min(100, 3 ** (attempt - 1))
        for _ in range(50):
            assert 0 <= policy.delay(attempt, rng) <= ceiling


def test_attempt_must_be_positive() -> None:
    with pytest.raises(ValueError, match="attempt"):
        RetryPolicy().delay(0)


async def test_sleep_zero() -> None:
    await RetryPolicy(base_delay=0, jitter=False).sleep(1)
