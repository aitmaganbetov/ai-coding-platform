"""Checkpoint Manager: integrity-checked recovery points (ТЗ §5, NFR-03)."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_coding.persistence.models import Checkpoint, Run, Task


class CheckpointIntegrityError(Exception):
    """A checkpoint snapshot does not match its stored digest."""


def _digest(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CheckpointManager:
    """Saves a snapshot after each confirmed step; loads the latest valid one."""

    @staticmethod
    def snapshot(session: Session, run: Run) -> dict[str, Any]:
        """Snapshot of run state and every task's status/output digest."""
        tasks = session.scalars(select(Task).where(Task.run_id == run.id).order_by(Task.key))
        return {
            "run_state": run.state,
            "tasks": {
                t.key: {
                    "status": t.status,
                    "attempts": t.attempts,
                    "output_sha256": _digest(t.output) if t.output is not None else None,
                }
                for t in tasks
            },
        }

    def save(
        self, session: Session, run: Run, label: str, *, task_id: uuid.UUID | None = None
    ) -> Checkpoint:
        """Persist a checkpoint in the caller's transaction."""
        snap = self.snapshot(session, run)
        seq = (
            session.scalar(select(func.max(Checkpoint.seq)).where(Checkpoint.run_id == run.id)) or 0
        ) + 1
        checkpoint = Checkpoint(
            run_id=run.id,
            task_id=task_id,
            seq=seq,
            label=label,
            snapshot=snap,
            sha256=_digest(snap),
        )
        session.add(checkpoint)
        return checkpoint

    @staticmethod
    def latest(session: Session, run_id: uuid.UUID) -> Checkpoint | None:
        """Most recent checkpoint, verified; raises on corruption."""
        checkpoint = session.scalar(
            select(Checkpoint)
            .where(Checkpoint.run_id == run_id)
            .order_by(Checkpoint.seq.desc())
            .limit(1)
        )
        if checkpoint is not None and _digest(checkpoint.snapshot) != checkpoint.sha256:
            raise CheckpointIntegrityError(f"checkpoint {checkpoint.seq} of run {run_id} corrupted")
        return checkpoint
