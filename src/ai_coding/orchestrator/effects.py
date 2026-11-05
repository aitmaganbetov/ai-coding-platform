"""Effect ledger: exactly-once side effects across crashes (ADR-0006, AC-04).

Protocol for an effect with key ``K``:

1. ledger has ``K`` *applied* → return the stored result, do nothing;
2. ledger has ``K`` *intent* (we crashed between doing and recording) → call the
   ``probe``: if the effect is observable (e.g. a commit with the task trailer
   exists) record it as applied and return; otherwise perform it again;
3. otherwise record *intent*, perform, record *applied*.

Each ledger write is its own committed transaction so that the intent survives a
crash of the worker process.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_coding.persistence.db import Database
from ai_coding.persistence.models import Effect
from ai_coding.persistence.types import utcnow

EffectResult = dict[str, Any]

INTENT = "intent"
APPLIED = "applied"


class EffectLedger:
    """Ledger bound to one run/task."""

    def __init__(self, db: Database, run_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
        self._db = db
        self._run_id = run_id
        self._task_id = task_id

    def _load(self, key: str) -> Effect | None:
        with self._db.session() as s:
            return s.scalar(select(Effect).where(Effect.run_id == self._run_id, Effect.key == key))

    def _mark_applied(self, key: str, result: EffectResult) -> None:
        with self._db.session() as s:
            effect = s.scalar(
                select(Effect).where(Effect.run_id == self._run_id, Effect.key == key)
            )
            if effect is None:  # pragma: no cover - intent is always written first
                effect = Effect(run_id=self._run_id, task_id=self._task_id, key=key, status=INTENT)
                s.add(effect)
            effect.status = APPLIED
            effect.result = result
            effect.applied_at = utcnow()

    def apply(
        self,
        key: str,
        action: Callable[[], EffectResult],
        *,
        probe: Callable[[], EffectResult | None] | None = None,
    ) -> EffectResult:
        """Perform ``action`` at most once per ``key`` within the run."""
        if not key or len(key) > 300:
            raise ValueError("effect key must be 1..300 characters")
        effect = self._load(key)
        if effect is not None and effect.status == APPLIED:
            return dict(effect.result or {})
        if effect is not None and probe is not None:
            observed = probe()
            if observed is not None:
                self._mark_applied(key, observed)
                return observed
        if effect is None:
            try:
                with self._db.session() as s:
                    s.add(
                        Effect(run_id=self._run_id, task_id=self._task_id, key=key, status=INTENT)
                    )
            except IntegrityError:  # concurrent writer recorded the intent first
                return self.apply(key, action, probe=probe)
        result = action()
        self._mark_applied(key, result)
        return result

    def status(self, key: str) -> str | None:
        """``intent``, ``applied`` or ``None``."""
        effect = self._load(key)
        return effect.status if effect else None
