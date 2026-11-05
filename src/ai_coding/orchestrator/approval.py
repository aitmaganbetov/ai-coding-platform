"""Approval Manager: human-in-the-loop requests and decisions (ТЗ §5, FR-17, AC-10)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_coding.domain.enums import ApprovalOperation, ApprovalStatus, Outcome, Role
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.errors import ApprovalDeniedError, ApprovalError
from ai_coding.persistence.models import Approval, Run
from ai_coding.persistence.types import utcnow

APPROVER_ROLES: frozenset[Role] = frozenset({Role.OWNER, Role.MAINTAINER})
DEFAULT_TTL = timedelta(days=7)


class ApprovalManager:
    """Creates, decides and expires approval requests; every step is audited."""

    def __init__(self, audit: AuditLogger, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._audit = audit
        self._ttl = ttl

    def request(
        self,
        session: Session,
        run: Run,
        operation: ApprovalOperation,
        *,
        reason: str,
        requested_by: str,
        task_id: uuid.UUID | None = None,
    ) -> Approval:
        """Create (or return the existing pending/approved) request for this run/task/op."""
        existing = session.scalar(
            select(Approval).where(
                Approval.run_id == run.id,
                Approval.task_id == task_id if task_id else Approval.task_id.is_(None),
                Approval.operation == operation.value,
                Approval.status.in_([ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]),
            )
        )
        if existing is not None:
            return existing
        approval = Approval(
            run_id=run.id,
            task_id=task_id,
            operation=operation.value,
            status=ApprovalStatus.PENDING.value,
            reason=reason,
            requested_by=requested_by,
            expires_at=utcnow() + self._ttl,
        )
        session.add(approval)
        session.flush()
        self._audit.record(
            session,
            actor=requested_by,
            action="approval.request",
            outcome=Outcome.SUCCESS,
            project_id=run.project_id,
            run_id=run.id,
            target=operation.value,
            details={"approval_id": str(approval.id), "reason": reason},
        )
        return approval

    def decide(
        self,
        session: Session,
        approval_id: uuid.UUID,
        *,
        approve: bool,
        actor: str,
        role: Role,
        note: str | None = None,
    ) -> Approval:
        """Approve or reject; only Owner/Maintainer may decide (ТЗ §3)."""
        approval = session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalError(f"approval {approval_id} not found")
        run = session.get(Run, approval.run_id)
        if run is None:  # pragma: no cover - FK guarantees the run exists
            raise ApprovalError(f"run of approval {approval_id} not found")
        if role not in APPROVER_ROLES:
            # The caller's transaction rolls back on this error, so the denial is audited
            # by the caller in its own transaction (see Orchestrator.decide).
            raise ApprovalDeniedError(f"role '{role}' cannot decide approvals")
        if approval.status != ApprovalStatus.PENDING.value:
            raise ApprovalError(f"approval {approval_id} is already {approval.status}")
        now = utcnow()
        if approval.expires_at is not None and approval.expires_at < now:
            approval.status = ApprovalStatus.EXPIRED.value
            raise ApprovalError(f"approval {approval_id} expired")
        approval.status = (ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED).value
        approval.decided_by = actor
        approval.decided_role = role.value
        approval.decided_at = now
        approval.decision_note = note
        self._audit.record(
            session,
            actor=actor,
            actor_role=role.value,
            action="approval.approve" if approve else "approval.reject",
            outcome=Outcome.SUCCESS,
            project_id=run.project_id,
            run_id=run.id,
            target=approval.operation,
            details={"approval_id": str(approval.id), "note": note},
        )
        return approval

    @staticmethod
    def for_run(session: Session, run_id: uuid.UUID) -> list[Approval]:
        """All approvals of a run, oldest first."""
        return list(
            session.scalars(
                select(Approval).where(Approval.run_id == run_id).order_by(Approval.requested_at)
            )
        )

    def expire_overdue(self, session: Session, run_id: uuid.UUID) -> int:
        """Mark pending approvals past their deadline as expired."""
        now = utcnow()
        count = 0
        for approval in self.for_run(session, run_id):
            if (
                approval.status == ApprovalStatus.PENDING.value
                and approval.expires_at is not None
                and approval.expires_at < now
            ):
                approval.status = ApprovalStatus.EXPIRED.value
                count += 1
        return count

    def summary(self, session: Session, run_id: uuid.UUID) -> dict[ApprovalStatus, int]:
        """Count approvals by status."""
        counts = dict.fromkeys(ApprovalStatus, 0)
        for approval in self.for_run(session, run_id):
            counts[ApprovalStatus(approval.status)] += 1
        return counts
