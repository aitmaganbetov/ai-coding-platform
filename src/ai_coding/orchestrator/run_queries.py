"""Read side of the orchestrator: run snapshots, journals and audit trails (ADR-0006).

Nothing here changes a run; the engine and the CLI/API read through it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_coding.domain.enums import RunState, TaskStatus
from ai_coding.orchestrator.aggregator import ResultAggregator, RunSummary
from ai_coding.orchestrator.approval import ApprovalManager
from ai_coding.orchestrator.errors import RunNotFoundError
from ai_coding.persistence.db import Database
from ai_coding.persistence.models import AuditEvent, Run, RunEvent, Task


def get_run(session: Session, run_id: uuid.UUID) -> Run:
    """The run row, or :class:`RunNotFoundError`."""
    run = session.get(Run, run_id)
    if run is None:
        raise RunNotFoundError(f"run {run_id} not found")
    return run


@dataclass(frozen=True, slots=True)
class RunStatus:
    """Snapshot of a run for CLI/API."""

    id: uuid.UUID
    state: RunState
    mode: str
    goal: str
    error: str | None
    lease_owner: str | None
    tasks: tuple[dict[str, Any], ...]
    approvals: tuple[dict[str, Any], ...]
    summary: RunSummary

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "id": str(self.id),
            "state": self.state.value,
            "mode": self.mode,
            "goal": self.goal,
            "error": self.error,
            "lease_owner": self.lease_owner,
            "tasks": list(self.tasks),
            "approvals": list(self.approvals),
            "summary": self.summary.to_dict(),
        }


class RunQueries:
    """Read-only views of runs."""

    def __init__(self, db: Database, approvals: ApprovalManager) -> None:
        self.db = db
        self.approvals = approvals

    def resolve_run_id(self, ref: str) -> uuid.UUID:
        """Full id from the full UUID, its short id (tail) or any unique prefix."""
        ref = ref.strip().lower()
        if not ref:
            raise RunNotFoundError("empty run id")
        with self.db.session() as session:
            matches = [
                rid
                for rid in session.scalars(select(Run.id))
                if str(rid).startswith(ref) or str(rid).endswith(ref)
            ]
        if not matches:
            raise RunNotFoundError(f"no run matches {ref!r}")
        if len(matches) > 1:
            raise RunNotFoundError(f"{ref!r} is ambiguous ({len(matches)} runs)")
        return matches[0]

    def status(self, run_id: uuid.UUID) -> RunStatus:
        """Current snapshot of a run."""
        with self.db.session() as session:
            run = get_run(session, run_id)
            tasks = session.scalars(select(Task).where(Task.run_id == run_id).order_by(Task.key))
            approvals = self.approvals.for_run(session, run_id)
            return RunStatus(
                id=run.id,
                state=RunState(run.state),
                mode=run.mode,
                goal=run.goal,
                error=run.error,
                lease_owner=run.lease_owner,
                tasks=tuple(
                    {
                        "key": t.key,
                        "capability": t.capability,
                        "phase": t.phase,
                        "status": t.status,
                        "attempts": t.attempts,
                        "error": t.error,
                    }
                    for t in tasks
                ),
                approvals=tuple(
                    {
                        "id": str(a.id),
                        "operation": a.operation,
                        "status": a.status,
                        "reason": a.reason,
                        "decided_by": a.decided_by,
                    }
                    for a in approvals
                ),
                summary=ResultAggregator.summarize(session, run_id),
            )

    def task_output(self, run_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        """Stored (redacted) output of a succeeded task, or ``None``."""
        with self.db.session() as session:
            get_run(session, run_id)
            task = session.scalars(
                select(Task).where(Task.run_id == run_id, Task.key == key)
            ).first()
            if task is None or task.status != TaskStatus.SUCCEEDED.value:
                return None
            return dict(task.output or {})

    def task_input(self, run_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        """Input a task was planned with (e.g. the full text a goal only abbreviates)."""
        with self.db.session() as session:
            get_run(session, run_id)
            task = session.scalars(
                select(Task).where(Task.run_id == run_id, Task.key == key)
            ).first()
            return None if task is None else dict(task.input or {})

    @staticmethod
    def list_runs(session: Session, project_id: uuid.UUID, limit: int) -> list[dict[str, Any]]:
        """Most recent runs of a project."""
        rows = session.scalars(
            select(Run)
            .where(Run.project_id == project_id)
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
        return [
            {
                "id": str(r.id),
                "state": r.state,
                "mode": r.mode,
                "goal": r.goal,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    def events(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        """Transition journal of a run."""
        with self.db.session() as session:
            get_run(session, run_id)
            rows = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            )
            return [
                {
                    "seq": e.seq,
                    "from": e.from_state,
                    "to": e.to_state,
                    "actor": e.actor,
                    "reason": e.reason,
                    "at": e.created_at.isoformat(),
                }
                for e in rows
            ]

    def audit_trail(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        """Audit events of a run, oldest first (AC-10 export)."""
        with self.db.session() as session:
            get_run(session, run_id)
            rows = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.run_id == run_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
            return [
                {
                    "at": e.created_at.isoformat(),
                    "actor": e.actor,
                    "actor_role": e.actor_role,
                    "project_id": str(e.project_id) if e.project_id else None,
                    "run_id": str(e.run_id) if e.run_id else None,
                    "action": e.action,
                    "target": e.target,
                    "outcome": e.outcome,
                    "details": e.details,
                }
                for e in rows
            ]
