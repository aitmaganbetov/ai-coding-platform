"""Approvals of a run (ТЗ §5 Approval Manager, FR-09): what a run needs, what was
decided, and the policy that follows from it."""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_coding.domain.enums import ApprovalOperation, ApprovalStatus, Outcome, Role
from ai_coding.orchestrator.approval import ApprovalManager
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.errors import ApprovalDeniedError, ApprovalError
from ai_coding.orchestrator.policy import PolicyEngine, PolicyOptions, Verdict
from ai_coding.orchestrator.run_queries import get_run
from ai_coding.persistence.db import Database
from ai_coding.persistence.models import Approval, ProjectMember, Run, Task, User

ApprovalState = Literal["pending", "rejected", "approved"]


def role_of(session: Session, actor: str, project_id: uuid.UUID) -> Role | None:
    """RBAC role of ``actor`` in the project (``None`` if not a member)."""
    row = session.execute(
        select(ProjectMember.role)
        .join(User, User.id == ProjectMember.user_id)
        .where(User.username == actor, ProjectMember.project_id == project_id)
    ).first()
    return Role(row[0]) if row else None


class RunApprovals:
    """Requests, decisions and the effective policy of runs."""

    def __init__(
        self,
        db: Database,
        approvals: ApprovalManager,
        audit: AuditLogger,
        base_policy: PolicyEngine,
        actor: str,
    ) -> None:
        self.db = db
        self.approvals = approvals
        self.audit = audit
        self.base_policy = base_policy
        self.actor = actor

    def policy_for(self, session: Session, run: Run) -> PolicyEngine:
        """The project policy with this run's options and the operations approved so far."""
        granted = frozenset(
            ApprovalOperation(a.operation)
            for a in self.approvals.for_run(session, run.id)
            if a.status == ApprovalStatus.APPROVED.value
        )
        return self.base_policy.with_options(
            PolicyOptions(
                require_approval_all=bool(run.options.get("require_approval")), granted=granted
            )
        )

    def request_for_plan(self, run_id: uuid.UUID, policy: PolicyEngine) -> bool:
        """Create approvals for steps that declare one; ``True`` if any is still pending."""
        pending = False
        with self.db.session() as session:
            run = get_run(session, run_id)
            tasks = session.scalars(
                select(Task).where(Task.run_id == run.id, Task.requires_approval.is_not(None))
            )
            for task in tasks:
                op = ApprovalOperation(task.requires_approval or "")
                decision = policy.operation(op)
                if decision.verdict is Verdict.REQUIRE_APPROVAL:
                    approval = self.approvals.request(
                        session,
                        run,
                        op,
                        reason=f"step '{task.key}': {decision.reason}",
                        requested_by=self.actor,
                        task_id=task.id,
                    )
                    pending = pending or approval.status == ApprovalStatus.PENDING.value
        return pending

    def request_for_task(
        self, run_id: uuid.UUID, task_id: uuid.UUID, op: ApprovalOperation, reason: str
    ) -> None:
        """An approval a running step asked for (it pauses until a human decides)."""
        with self.db.session() as session:
            run = get_run(session, run_id)
            self.approvals.request(
                session, run, op, reason=reason, requested_by=self.actor, task_id=task_id
            )

    def state(self, run_id: uuid.UUID) -> ApprovalState:
        """Overall decision on the run's approvals (overdue ones expire first)."""
        with self.db.session() as session:
            self.approvals.expire_overdue(session, run_id)
            counts = self.approvals.summary(session, run_id)
        if counts[ApprovalStatus.REJECTED] or counts[ApprovalStatus.EXPIRED]:
            return "rejected"
        if counts[ApprovalStatus.PENDING]:
            return "pending"
        return "approved"

    def expire_pending(self, session: Session, run_id: uuid.UUID) -> None:
        """A cancelled run's pending approvals can no longer be decided."""
        for approval in self.approvals.for_run(session, run_id):
            if approval.status == ApprovalStatus.PENDING.value:
                approval.status = ApprovalStatus.EXPIRED.value

    def decide(
        self,
        run_id: uuid.UUID,
        *,
        approve: bool,
        approval_id: uuid.UUID | None,
        note: str | None,
        actor: str,
    ) -> list[Approval]:
        """Approve or reject pending approvals (all, or one by id); a refusal is audited."""
        try:
            return self._decide(
                run_id, approve=approve, approval_id=approval_id, note=note, actor=actor
            )
        except ApprovalError as exc:
            with self.db.session() as session:
                run = get_run(session, run_id)
                self.audit.record(
                    session,
                    actor=actor,
                    actor_role=(
                        r.value if (r := role_of(session, actor, run.project_id)) else None
                    ),
                    action="approval.approve" if approve else "approval.reject",
                    outcome=Outcome.DENIED,
                    project_id=run.project_id,
                    run_id=run.id,
                    details={"error": str(exc)},
                )
            raise

    def _decide(
        self,
        run_id: uuid.UUID,
        *,
        approve: bool,
        approval_id: uuid.UUID | None,
        note: str | None,
        actor: str,
    ) -> list[Approval]:
        with self.db.session() as session:
            run = get_run(session, run_id)
            role = role_of(session, actor, run.project_id)
            if role is None:
                raise ApprovalDeniedError(f"{actor} is not a member of the project")
            pending = [
                a
                for a in self.approvals.for_run(session, run_id)
                if a.status == ApprovalStatus.PENDING.value
                and (approval_id is None or a.id == approval_id)
            ]
            if not pending:
                raise ApprovalError("no pending approval matches")
            return [
                self.approvals.decide(
                    session, a.id, approve=approve, actor=actor, role=role, note=note
                )
                for a in pending
            ]
