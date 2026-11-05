"""Budget tracking for tokens, cost and wall time per run (FR-15)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from ai_coding.config.models import BudgetsConfig
from ai_coding.orchestrator.errors import BudgetExceededError
from ai_coding.persistence.types import utcnow


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Consumption so far."""

    tokens: int
    cost_usd: float
    elapsed_seconds: float


class BudgetTracker:
    """Thread-safe accumulator that refuses work beyond the configured limits."""

    def __init__(
        self,
        limits: BudgetsConfig,
        *,
        started_at: datetime | None = None,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self._limits = limits
        self._started = started_at or utcnow()
        self._tokens = tokens
        self._cost = cost_usd
        self._lock = threading.Lock()

    def snapshot(self) -> BudgetSnapshot:
        """Current consumption."""
        with self._lock:
            elapsed = (utcnow() - self._started).total_seconds()
            return BudgetSnapshot(self._tokens, self._cost, elapsed)

    def check(self, *, extra_tokens: int = 0, extra_cost_usd: float = 0.0) -> None:
        """Raise :class:`BudgetExceededError` if the next spend would exceed a limit."""
        snap = self.snapshot()
        if snap.tokens + extra_tokens > self._limits.max_tokens_per_run:
            raise BudgetExceededError(
                f"token budget exceeded: {snap.tokens + extra_tokens} > "
                f"{self._limits.max_tokens_per_run}"
            )
        if snap.cost_usd + extra_cost_usd > self._limits.max_cost_usd_per_run:
            raise BudgetExceededError(
                f"cost budget exceeded: ${snap.cost_usd + extra_cost_usd:.4f} > "
                f"${self._limits.max_cost_usd_per_run:.2f}"
            )
        if snap.elapsed_seconds > self._limits.max_duration_minutes * 60:
            raise BudgetExceededError(
                f"time budget exceeded: {snap.elapsed_seconds:.0f}s > "
                f"{self._limits.max_duration_minutes}min"
            )

    def add(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        """Record consumption (after the spend happened)."""
        if tokens < 0 or cost_usd < 0:
            raise ValueError("consumption cannot be negative")
        with self._lock:
            self._tokens += tokens
            self._cost += cost_usd
