"""Result Aggregator: one summary with evidence links (ТЗ §5).

The run outcome is derived only from task statuses decided by the orchestrator
(handler completion + success criteria). Agent ``confidence`` is reported but can
never turn a failed step into a success.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_coding.domain.enums import TaskStatus
from ai_coding.persistence.models import AgentExecution, Artifact, Run, Task


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """Per-task line of the summary."""

    key: str
    status: TaskStatus
    attempts: int
    error: str | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Aggregated result of a run."""

    run_id: uuid.UUID
    state: str
    succeeded: bool
    counts: dict[str, int]
    tasks: tuple[TaskSummary, ...]
    artifacts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "run_id": str(self.run_id),
            "state": self.state,
            "succeeded": self.succeeded,
            "counts": self.counts,
            "tasks": [
                {
                    "key": t.key,
                    "status": t.status.value,
                    "attempts": t.attempts,
                    "error": t.error,
                    "confidence": t.confidence,
                }
                for t in self.tasks
            ],
            "artifacts": list(self.artifacts),
        }


class ResultAggregator:
    """Builds :class:`RunSummary` from persisted state."""

    @staticmethod
    def summarize(session: Session, run_id: uuid.UUID) -> RunSummary:
        """Summary of ``run_id``."""
        run = session.get(Run, run_id)
        if run is None:
            raise KeyError(str(run_id))
        tasks = list(session.scalars(select(Task).where(Task.run_id == run_id).order_by(Task.key)))
        confidence: dict[uuid.UUID, float | None] = {}
        for execution in session.scalars(
            select(AgentExecution)
            .where(AgentExecution.task_id.in_([t.id for t in tasks]))
            .order_by(AgentExecution.attempt)
        ):
            confidence[execution.task_id] = execution.confidence
        counts = Counter(t.status for t in tasks)
        lines = tuple(
            TaskSummary(t.key, TaskStatus(t.status), t.attempts, t.error, confidence.get(t.id))
            for t in tasks
        )
        relevant = [s for s in lines if s.status is not TaskStatus.SKIPPED]
        succeeded = bool(relevant) and all(s.status is TaskStatus.SUCCEEDED for s in relevant)
        artifacts = tuple(
            {"name": a.name, "uri": a.uri, "sha256": a.sha256, "media_type": a.media_type}
            for a in session.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
            )
        )
        return RunSummary(
            run_id=run.id,
            state=run.state,
            succeeded=succeeded and run.state == "COMPLETED",
            counts=dict(sorted(counts.items())),
            tasks=lines,
            artifacts=artifacts,
        )
