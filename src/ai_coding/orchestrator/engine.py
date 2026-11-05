"""Orchestrator Engine: drives runs through the FSM and executes the task DAG.

Design (ADR-0006):

* All state lives in the database; the process holds only a *lease* on the run
  (renewed by a heartbeat). A crashed worker loses the lease and any worker can
  resume the run from the persisted state (AC-04, NFR-03).
* Each task attempt is recorded before it starts; success is recorded together
  with a checkpoint in one transaction. Side effects go through the effect ledger,
  so re-executing an interrupted task never repeats an applied effect.
* Run outcome is decided only from deterministic task statuses (ТЗ §5).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import socket
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import (
    ApprovalOperation,
    ApprovalStatus,
    Mode,
    Outcome,
    ProviderName,
    Role,
    RunState,
    TaskPhase,
    TaskStatus,
)
from ai_coding.llm.base import TokenUsage
from ai_coding.observability import bind_run, get_logger
from ai_coding.orchestrator.aggregator import ResultAggregator, RunSummary
from ai_coding.orchestrator.approval import ApprovalManager
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.budget import BudgetTracker
from ai_coding.orchestrator.checkpoint import CheckpointManager
from ai_coding.orchestrator.effects import EffectLedger
from ai_coding.orchestrator.errors import (
    ApprovalDeniedError,
    ApprovalError,
    ApprovalRequiredError,
    BudgetExceededError,
    ConcurrencyConflictError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NoRouteError,
    PolicyDeniedError,
    RunLeaseError,
    RunNotFoundError,
    SuccessCriteriaError,
)
from ai_coding.orchestrator.planner import Plan
from ai_coding.orchestrator.policy import Decision, PolicyEngine, PolicyOptions, Verdict
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.orchestrator.retry import ErrorClass, RetryPolicy, classify
from ai_coding.orchestrator.router import AgentRouter
from ai_coding.orchestrator.states import TERMINAL_STATES, StateManager
from ai_coding.persistence.artifacts import ArtifactRef, LocalArtifactStore
from ai_coding.persistence.db import Database
from ai_coding.persistence.models import (
    AgentExecution,
    Approval,
    Artifact,
    AuditEvent,
    ModelUsage,
    Project,
    ProjectMember,
    Run,
    RunEvent,
    Task,
    TaskDependency,
    User,
)
from ai_coding.persistence.types import utcnow
from ai_coding.security import redact

_log = get_logger("ai_coding.orchestrator")
_FAILED_DEP = {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
_PHASE_STATE = {
    TaskPhase.DISCOVERY: RunState.DISCOVERY,
    TaskPhase.EXECUTION: RunState.RUNNING,
    TaskPhase.VERIFICATION: RunState.VERIFYING,
}
_MAX_ERROR_CHARS = 4000


def local_actor() -> str:
    """Identity of the local CLI user (``local:<username>``)."""
    try:
        name = getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - no passwd entry
        name = "unknown"
    return f"local:{name}"


def short_id(value: uuid.UUID | str) -> str:
    """Display id: the random tail of a UUIDv7 (its prefix is a timestamp and collides)."""
    return str(value)[-12:]


def _err(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return redact(text)[:_MAX_ERROR_CHARS]


class PhaseOutcome(StrEnum):
    """Result of executing one phase of the DAG."""

    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET = "budget"
    APPROVAL = "approval"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Per-run options (CLI flags)."""

    require_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON form stored on the run."""
        return {"require_approval": self.require_approval}


@dataclass(frozen=True, slots=True)
class StepPreview:
    """Dry-run view of a step: who would run it and what policy says."""

    key: str
    phase: TaskPhase
    capability: str
    handler: str | None
    route_error: str | None
    approval: Decision | None


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


@dataclass(slots=True)
class _TaskOutcome:
    kind: PhaseOutcome
    operation: ApprovalOperation | None = None
    reason: str = ""


@dataclass(slots=True)
class _RunContext:
    run_id: uuid.UUID
    policy: PolicyEngine
    budget: BudgetTracker
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    lease_lost: bool = False


class Orchestrator:
    """Facade over planner, state, policy, approvals, execution and recovery."""

    def __init__(
        self,
        *,
        db: Database,
        config: AICodingConfig,
        project_root: Path,
        registry: HandlerRegistry,
        actor: str | None = None,
        worker_id: str | None = None,
        lease_ttl: float = 30.0,
        retry_policy: RetryPolicy | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        self.db = db
        self.config = config
        self.project_root = project_root.resolve()
        self.registry = registry
        self.actor = actor or local_actor()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lease_ttl = lease_ttl
        self.retry = retry_policy or RetryPolicy()
        self.poll_interval = poll_interval
        self.audit = AuditLogger()
        self.states = StateManager(self.audit)
        self.approvals = ApprovalManager(self.audit)
        self.checkpoints = CheckpointManager()
        self.base_policy = PolicyEngine(config)
        artifacts_root = Path(config.artifacts.path)
        if not artifacts_root.is_absolute():
            artifacts_root = self.project_root / artifacts_root
        self.artifacts = LocalArtifactStore(artifacts_root)

    def close(self) -> None:
        """Release database connections."""
        self.db.dispose()

    # ------------------------------------------------------------ project --
    def ensure_project(self, session: Session) -> Project:
        """Project row for this configuration; the local actor is its Owner."""
        project = session.scalar(select(Project).where(Project.name == self.config.project.name))
        if project is None:
            project = Project(name=self.config.project.name, root_path=str(self.project_root))
            session.add(project)
            session.flush()
        user = session.scalar(select(User).where(User.username == self.actor))
        if user is None:
            user = User(username=self.actor, display_name=self.actor)
            session.add(user)
            session.flush()
        member = session.get(ProjectMember, (project.id, user.id))
        if member is None:
            session.add(
                ProjectMember(project_id=project.id, user_id=user.id, role=Role.OWNER.value)
            )
            session.flush()
        return project

    def role_of(self, session: Session, actor: str, project_id: uuid.UUID) -> Role | None:
        """RBAC role of ``actor`` in the project (``None`` if not a member)."""
        row = session.execute(
            select(ProjectMember.role)
            .join(User, User.id == ProjectMember.user_id)
            .where(User.username == actor, ProjectMember.project_id == project_id)
        ).first()
        return Role(row[0]) if row else None

    # ----------------------------------------------------------- planning --
    def _policy_for(self, session: Session, run: Run) -> PolicyEngine:
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

    def preview(self, plan: Plan, options: RunOptions | None = None) -> list[StepPreview]:
        """Dry-run: routing and policy decisions without writing anything (AC-10)."""
        policy = self.base_policy.with_options(
            PolicyOptions(require_approval_all=(options or RunOptions()).require_approval)
        )
        router = AgentRouter(self.registry, policy)
        previews: list[StepPreview] = []
        for key in plan.order:
            step = plan.step(key)
            handler: str | None = None
            error: str | None = None
            try:
                handler = router.route(step.capability, max_cost_usd=step.budget_usd).handler.name
            except NoRouteError as exc:
                error = str(exc)
            approval = policy.operation(step.requires_approval) if step.requires_approval else None
            previews.append(StepPreview(key, step.phase, step.capability, handler, error, approval))
        return previews

    def create_run(
        self,
        plan: Plan,
        *,
        mode: Mode | None = None,
        goal: str = "",
        idempotency_key: str | None = None,
        options: RunOptions | None = None,
    ) -> uuid.UUID:
        """Persist a run in CREATED with its DAG. Idempotent by ``idempotency_key``."""
        options = options or RunOptions()
        digest = plan.digest()
        with self.db.session() as session:
            project = self.ensure_project(session)
            if idempotency_key:
                existing = session.scalar(
                    select(Run).where(
                        Run.project_id == project.id, Run.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    if existing.options.get("plan_sha256") != digest:
                        raise IdempotencyConflictError(
                            f"idempotency key {idempotency_key!r} was used for a different plan"
                        )
                    return existing.id
            run = Run(
                project_id=project.id,
                mode=(mode or self.config.mode).value,
                goal=goal,
                state=RunState.CREATED.value,
                idempotency_key=idempotency_key,
                requested_by=self.actor,
                options={**options.to_dict(), "plan_sha256": digest},
                budget=self.config.budgets.model_dump(mode="json"),
            )
            session.add(run)
            session.flush()
            ids: dict[str, uuid.UUID] = {}
            for key in plan.order:
                step = plan.step(key)
                task = Task(
                    run_id=run.id,
                    key=step.key,
                    name=step.name or step.key,
                    phase=step.phase.value,
                    capability=step.capability,
                    status=TaskStatus.PENDING.value,
                    max_attempts=step.max_attempts,
                    timeout_seconds=step.timeout_seconds,
                    requires_approval=step.requires_approval.value
                    if step.requires_approval
                    else None,
                    budget_usd=step.budget_usd,
                    success_criteria=[dict(step.success_criteria)] if step.success_criteria else [],
                    input=dict(step.input),
                )
                session.add(task)
                session.flush()
                ids[key] = task.id
            for step in plan.steps:
                for dep in set(step.depends_on):
                    session.add(TaskDependency(task_id=ids[step.key], depends_on_id=ids[dep]))
            session.add(
                RunEvent(
                    run_id=run.id,
                    seq=0,
                    from_state=None,
                    to_state=RunState.CREATED.value,
                    idempotency_key="created",
                    actor=self.actor,
                )
            )
            self.audit.record(
                session,
                actor=self.actor,
                action="run.create",
                outcome=Outcome.SUCCESS,
                project_id=project.id,
                run_id=run.id,
                target=run.mode,
                details={
                    "goal": goal,
                    "steps": len(plan.steps),
                    "plan_sha256": digest,
                    "policy_sha256": self.base_policy.digest,
                    "options": options.to_dict(),
                },
            )
            return run.id

    # ----------------------------------------------------------- queries --
    def _get_run(self, session: Session, run_id: uuid.UUID) -> Run:
        run = session.get(Run, run_id)
        if run is None:
            raise RunNotFoundError(f"run {run_id} not found")
        return run

    def resolve_run_id(self, ref: str) -> uuid.UUID:
        """Full id from the full UUID, its :func:`short_id` or any unique prefix."""
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
            run = self._get_run(session, run_id)
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

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent runs of this project."""
        with self.db.session() as session:
            project = self.ensure_project(session)
            rows = session.scalars(
                select(Run)
                .where(Run.project_id == project.id)
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
            self._get_run(session, run_id)
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
            self._get_run(session, run_id)
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

    # ------------------------------------------------------------- leases --
    def _acquire_lease(self, run_id: uuid.UUID, *, force: bool = False) -> None:
        now = utcnow()
        cond = [Run.id == run_id]
        if not force:
            cond.append(
                or_(
                    Run.lease_owner.is_(None),
                    Run.lease_owner == self.worker_id,
                    Run.lease_expires_at < now,
                )
            )
        with self.db.session() as session:
            result = session.execute(
                update(Run)
                .where(*cond)
                .values(
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_ttl),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                run = self._get_run(session, run_id)
                raise RunLeaseError(
                    f"run {run_id} is leased by {run.lease_owner} until "
                    f"{run.lease_expires_at.isoformat() if run.lease_expires_at else '?'}"
                )

    def _renew_lease(self, run_id: uuid.UUID) -> bool:
        with self.db.session() as session:
            result = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.lease_owner == self.worker_id)
                .values(lease_expires_at=utcnow() + timedelta(seconds=self.lease_ttl))
                .execution_options(synchronize_session=False)
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    def _release_lease(self, run_id: uuid.UUID) -> None:
        with self.db.session() as session:
            session.execute(
                update(Run)
                .where(Run.id == run_id, Run.lease_owner == self.worker_id)
                .values(lease_owner=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )

    async def _heartbeat(self, rc: _RunContext) -> None:
        interval = max(self.lease_ttl / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            if not await asyncio.to_thread(self._renew_lease, rc.run_id):
                rc.lease_lost = True
                rc.cancel_event.set()
                _log.warning("lease lost", run_id=str(rc.run_id), worker=self.worker_id)
                return

    # -------------------------------------------------------- transitions --
    def _transition(
        self,
        run_id: uuid.UUID,
        target: RunState,
        *,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        for _ in range(5):
            try:
                with self.db.session() as session:
                    run = self._get_run(session, run_id)
                    key = f"auto:{run.state}->{target.value}:v{run.version}"
                    self.states.transition(
                        session, run, target, actor=self.actor, idempotency_key=key, reason=reason
                    )
                    if error is not None:
                        run.error = error
                    if target is RunState.PLANNED:
                        self.checkpoints.save(session, run, "planned")
                    if target in TERMINAL_STATES:
                        self.checkpoints.save(session, run, f"final:{target.value}")
            except ConcurrencyConflictError:
                continue
            else:
                return
        raise ConcurrencyConflictError(f"could not transition run {run_id} to {target}")

    # ---------------------------------------------------------- execution --
    async def execute(self, run_id: uuid.UUID) -> RunStatus:
        """Drive a run as far as possible (until terminal, approval or pause)."""
        await asyncio.to_thread(self._acquire_lease, run_id)
        return await self._drive(run_id)

    async def resume(self, run_id: uuid.UUID, *, force: bool = False) -> RunStatus:
        """Continue a run after a crash, pause or approval (FR-09, AC-04)."""
        with self.db.session() as session:
            run = self._get_run(session, run_id)
            state = RunState(run.state)
            if state in TERMINAL_STATES:
                raise InvalidTransitionError(f"run {run_id} is {state} and cannot be resumed")
        await asyncio.to_thread(self._acquire_lease, run_id, force=force)
        previous: RunState | None = None
        with self.db.session() as session:
            run = self._get_run(session, run_id)
            self.audit.record(
                session,
                actor=self.actor,
                action="run.resume",
                outcome=Outcome.SUCCESS,
                project_id=run.project_id,
                run_id=run.id,
                target=run.state,
                details={"force": force, "worker": self.worker_id},
            )
            if run.state == RunState.PAUSED.value:
                previous = StateManager.state_before_pause(session, run) or RunState.RUNNING
        if previous is not None:
            self._transition(run_id, previous, reason="resumed")
        return await self._drive(run_id)

    def cancel(self, run_id: uuid.UUID, *, reason: str = "cancelled by user") -> RunStatus:
        """Request cancellation; immediate if no live worker holds the run (FR-09)."""
        with self.db.session() as session:
            run = self._get_run(session, run_id)
            if RunState(run.state) in TERMINAL_STATES:
                raise InvalidTransitionError(f"run {run_id} is already {run.state}")
            run.cancel_requested = True
            live = (
                run.lease_owner is not None
                and run.lease_owner != self.worker_id
                and run.lease_expires_at is not None
                and run.lease_expires_at > utcnow()
            )
            self.audit.record(
                session,
                actor=self.actor,
                action="run.cancel",
                outcome=Outcome.SUCCESS,
                project_id=run.project_id,
                run_id=run.id,
                details={"reason": reason, "cooperative": live},
            )
        if not live:
            self._finalize_cancel(run_id, reason)
        return self.status(run_id)

    def _finalize_cancel(self, run_id: uuid.UUID, reason: str) -> None:
        with self.db.session() as session:
            for task in session.scalars(select(Task).where(Task.run_id == run_id)):
                if not TaskStatus(task.status).is_terminal:
                    task.status = TaskStatus.CANCELLED.value
                    task.finished_at = utcnow()
            for approval in self.approvals.for_run(session, run_id):
                if approval.status == ApprovalStatus.PENDING.value:
                    approval.status = ApprovalStatus.EXPIRED.value
        self._transition(run_id, RunState.CANCELLED, reason=reason)

    def decide(
        self,
        run_id: uuid.UUID,
        *,
        approve: bool,
        approval_id: uuid.UUID | None = None,
        note: str | None = None,
        actor: str | None = None,
    ) -> list[Approval]:
        """Approve or reject pending approvals of a run (all, or one by id)."""
        actor = actor or self.actor
        try:
            return self._decide(
                run_id, approve=approve, approval_id=approval_id, note=note, actor=actor
            )
        except ApprovalError as exc:
            with self.db.session() as session:
                run = self._get_run(session, run_id)
                self.audit.record(
                    session,
                    actor=actor,
                    actor_role=(
                        r.value if (r := self.role_of(session, actor, run.project_id)) else None
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
            run = self._get_run(session, run_id)
            role = self.role_of(session, actor, run.project_id)
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

    async def _drive(self, run_id: uuid.UUID) -> RunStatus:
        with self.db.session() as session:
            run = self._get_run(session, run_id)
            policy = self._policy_for(session, run)
            started = run.started_at or utcnow()
            tokens, cost = session.execute(
                select(
                    func.coalesce(func.sum(ModelUsage.input_tokens + ModelUsage.output_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
                ).where(ModelUsage.run_id == run_id)
            ).one()
        rc = _RunContext(
            run_id=run_id,
            policy=policy,
            budget=BudgetTracker(
                self.config.budgets, started_at=started, tokens=int(tokens), cost_usd=float(cost)
            ),
        )
        heartbeat = asyncio.create_task(self._heartbeat(rc))
        try:
            with bind_run(str(run_id), worker=self.worker_id):
                await self._loop(rc)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if not rc.lease_lost:
                await asyncio.to_thread(self._release_lease, run_id)
        return self.status(run_id)

    def _cancel_requested(self, run_id: uuid.UUID) -> bool:
        with self.db.session() as session:
            return bool(self._get_run(session, run_id).cancel_requested)

    async def _loop(self, rc: _RunContext) -> None:  # noqa: PLR0911, PLR0912 - FSM dispatch
        while True:
            if rc.lease_lost:
                return
            if self._cancel_requested(rc.run_id):
                self._finalize_cancel(rc.run_id, "cancelled by user")
                return
            with self.db.session() as session:
                state = RunState(self._get_run(session, rc.run_id).state)
            if state is RunState.CREATED:
                self._transition(rc.run_id, RunState.DISCOVERY)
            elif state is RunState.DISCOVERY:
                if not await self._phase_then(rc, TaskPhase.DISCOVERY, RunState.PLANNED):
                    return
            elif state is RunState.PLANNED:
                if self._request_plan_approvals(rc):
                    self._transition(rc.run_id, RunState.AWAITING_APPROVAL)
                    return
                self._transition(rc.run_id, RunState.RUNNING)
            elif state is RunState.AWAITING_APPROVAL:
                decision = self._approval_state(rc)
                if decision == "pending":
                    return
                if decision == "rejected":
                    self._finalize_cancel(rc.run_id, "approval rejected or expired")
                    return
                with self.db.session() as session:
                    rc.policy = self._policy_for(session, self._get_run(session, rc.run_id))
                self._transition(rc.run_id, RunState.RUNNING, reason="approved")
            elif state is RunState.RUNNING:
                if not await self._phase_then(rc, TaskPhase.EXECUTION, RunState.VERIFYING):
                    return
            elif state is RunState.VERIFYING:
                if not await self._phase_then(rc, TaskPhase.VERIFICATION, RunState.COMPLETED):
                    return
            else:  # PAUSED or terminal
                return

    async def _phase_then(self, rc: _RunContext, phase: TaskPhase, target: RunState) -> bool:
        outcome = await self._run_phase(rc, phase)
        if outcome.kind is PhaseOutcome.OK:
            self._transition(rc.run_id, target)
            return True
        if outcome.kind is PhaseOutcome.LEASE_LOST:
            return False
        if outcome.kind is PhaseOutcome.CANCELLED:
            self._finalize_cancel(rc.run_id, outcome.reason or "cancelled by user")
        elif outcome.kind is PhaseOutcome.BUDGET:
            self._transition(rc.run_id, RunState.PAUSED, reason=outcome.reason)
        elif outcome.kind is PhaseOutcome.APPROVAL and phase is TaskPhase.EXECUTION:
            self._transition(rc.run_id, RunState.AWAITING_APPROVAL, reason=outcome.reason)
        else:
            self._transition(
                rc.run_id, RunState.FAILED, reason=outcome.reason, error=outcome.reason
            )
        return False

    def _request_plan_approvals(self, rc: _RunContext) -> bool:
        """Create approvals for steps that declare one; ``True`` if any is still pending."""
        pending = False
        with self.db.session() as session:
            run = self._get_run(session, rc.run_id)
            tasks = session.scalars(
                select(Task).where(Task.run_id == run.id, Task.requires_approval.is_not(None))
            )
            for task in tasks:
                op = ApprovalOperation(task.requires_approval or "")
                decision = rc.policy.operation(op)
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

    def _approval_state(self, rc: _RunContext) -> str:
        with self.db.session() as session:
            self.approvals.expire_overdue(session, rc.run_id)
            counts = self.approvals.summary(session, rc.run_id)
        if counts[ApprovalStatus.REJECTED] or counts[ApprovalStatus.EXPIRED]:
            return "rejected"
        if counts[ApprovalStatus.PENDING]:
            return "pending"
        return "approved"

    # -------------------------------------------------------------- phase --
    def _load_graph(
        self, run_id: uuid.UUID
    ) -> tuple[dict[str, Task], dict[uuid.UUID, str], dict[str, list[str]]]:
        with self.db.session() as session:
            tasks = {t.key: t for t in session.scalars(select(Task).where(Task.run_id == run_id))}
            by_id = {t.id: k for k, t in tasks.items()}
            deps: dict[str, list[str]] = {k: [] for k in tasks}
            for edge in session.scalars(
                select(TaskDependency).where(TaskDependency.task_id.in_(list(by_id)))
            ):
                deps[by_id[edge.task_id]].append(by_id[edge.depends_on_id])
            return tasks, by_id, deps

    def _set_task(self, task_id: uuid.UUID, **values: Any) -> None:
        with self.db.session() as session:
            task = session.get(Task, task_id)
            if task is None:  # pragma: no cover - FK guarantees existence
                raise RunNotFoundError(f"task {task_id} vanished")
            for name, value in values.items():
                setattr(task, name, value)

    async def _run_phase(self, rc: _RunContext, phase: TaskPhase) -> _TaskOutcome:  # noqa: PLR0912
        tasks, _, deps = self._load_graph(rc.run_id)
        # A task left RUNNING belongs to an interrupted attempt (worker crash): retry it.
        for task in tasks.values():
            if task.phase == phase.value and task.status == TaskStatus.RUNNING.value:
                self._set_task(task.id, status=TaskStatus.PENDING.value)
        semaphore = asyncio.Semaphore(self.config.execution.limits.max_parallel_tasks)
        running: dict[asyncio.Task[_TaskOutcome], str] = {}
        outcomes: list[_TaskOutcome] = []
        awaiting_approval: set[str] = set()  # not relaunched until a human decides
        stop_launching = False
        while True:
            if rc.lease_lost:
                await self._cancel_running(running)
                return _TaskOutcome(PhaseOutcome.LEASE_LOST)
            if not stop_launching and self._cancel_requested(rc.run_id):
                rc.cancel_event.set()
                await self._cancel_running(running)
                return _TaskOutcome(PhaseOutcome.CANCELLED, reason="cancelled by user")
            tasks, _, deps = self._load_graph(rc.run_id)
            status = {k: TaskStatus(t.status) for k, t in tasks.items()}
            skipped_any = False
            if not stop_launching:
                for key, task in sorted(tasks.items()):
                    if task.phase != phase.value or status[key] is not TaskStatus.PENDING:
                        continue
                    if key in running.values() or key in awaiting_approval:
                        continue
                    blocked = [d for d in deps[key] if status[d] in _FAILED_DEP]
                    if blocked:
                        self._set_task(
                            task.id,
                            status=TaskStatus.SKIPPED.value,
                            error=f"dependency {blocked[0]} did not succeed",
                            finished_at=utcnow(),
                        )
                        status[key] = TaskStatus.SKIPPED
                        skipped_any = True
                        continue
                    if all(status[d] is TaskStatus.SUCCEEDED for d in deps[key]):
                        coro = self._execute_task(rc, task.id, semaphore)
                        running[asyncio.create_task(coro)] = key
            if not running:
                if skipped_any:
                    continue  # cascade skips to transitive dependents
                break
            done, _ = await asyncio.wait(
                running, timeout=self.poll_interval, return_when=asyncio.FIRST_COMPLETED
            )
            for finished in done:
                finished_key = running.pop(finished)
                outcome = finished.result()
                outcomes.append(outcome)
                if outcome.kind is PhaseOutcome.APPROVAL:
                    awaiting_approval.add(finished_key)
                if outcome.kind in {PhaseOutcome.BUDGET, PhaseOutcome.CANCELLED}:
                    stop_launching = True
        for kind in (
            PhaseOutcome.CANCELLED,
            PhaseOutcome.BUDGET,
            PhaseOutcome.APPROVAL,
            PhaseOutcome.FAILED,
        ):
            for outcome in outcomes:
                if outcome.kind is kind:
                    return outcome
        # Tasks skipped because of failures in an earlier attempt also fail the phase.
        tasks, _, _ = self._load_graph(rc.run_id)
        failed = [
            k
            for k, t in tasks.items()
            if t.phase == phase.value and t.status in {TaskStatus.FAILED.value}
        ]
        if failed:
            return _TaskOutcome(PhaseOutcome.FAILED, reason=f"task {failed[0]} failed")
        return _TaskOutcome(PhaseOutcome.OK)

    async def _cancel_running(self, running: Mapping[asyncio.Task[_TaskOutcome], str]) -> None:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    def _dependency_outputs(self, task_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        with self.db.session() as session:
            rows = session.execute(
                select(Task.key, Task.output)
                .join(TaskDependency, TaskDependency.depends_on_id == Task.id)
                .where(TaskDependency.task_id == task_id)
            )
            return {key: dict(output or {}) for key, output in rows}

    def _artifact_saver(
        self, run_id: uuid.UUID, task_id: uuid.UUID
    ) -> Callable[[str, bytes, str], ArtifactRef]:
        def save(name: str, data: bytes, media_type: str) -> ArtifactRef:
            ref = self.artifacts.put(redact(data.decode("utf-8")).encode("utf-8")
                                     if media_type.startswith(("text/", "application/json"))
                                     else data)  # fmt: skip
            with self.db.session() as session:
                exists = session.scalar(
                    select(Artifact).where(
                        Artifact.run_id == run_id,
                        Artifact.name == name,
                        Artifact.sha256 == ref.sha256,
                    )
                )
                if exists is None:
                    session.add(
                        Artifact(
                            run_id=run_id,
                            task_id=task_id,
                            name=name,
                            media_type=media_type,
                            sha256=ref.sha256,
                            size_bytes=ref.size_bytes,
                            uri=ref.uri,
                        )
                    )
            return ref

        return save

    async def _execute_task(  # noqa: PLR0915 - one attempt loop, many outcomes
        self, rc: _RunContext, task_id: uuid.UUID, semaphore: asyncio.Semaphore
    ) -> _TaskOutcome:
        async with semaphore:
            while True:
                with self.db.session() as session:
                    task = session.get(Task, task_id)
                    if task is None:  # pragma: no cover
                        raise RunNotFoundError(f"task {task_id} vanished")
                    attempt = task.attempts + 1
                    task.attempts = attempt
                    task.status = TaskStatus.RUNNING.value
                    task.started_at = task.started_at or utcnow()
                    key, capability = task.key, task.capability
                    timeout, max_attempts = task.timeout_seconds, task.max_attempts
                    criteria = list(task.success_criteria or [])
                    task_input = dict(task.input or {})
                    budget_usd = task.budget_usd
                try:
                    rc.budget.check()
                    route = AgentRouter(self.registry, rc.policy).route(
                        capability, max_cost_usd=budget_usd
                    )
                except BudgetExceededError as exc:
                    self._set_task(task_id, status=TaskStatus.PENDING.value, attempts=attempt - 1)
                    return _TaskOutcome(PhaseOutcome.BUDGET, reason=_err(exc))
                except NoRouteError as exc:
                    self._fail_task(task_id, _err(exc))
                    return _TaskOutcome(PhaseOutcome.FAILED, reason=f"task {key}: {_err(exc)}")
                handler = route.handler
                with self.db.session() as session:
                    execution = AgentExecution(
                        task_id=task_id, agent_name=handler.name, attempt=attempt, status="running"
                    )
                    session.add(execution)
                    session.flush()
                    execution_id = execution.id
                    tsk = session.get(Task, task_id)
                    if tsk is not None:
                        tsk.handler = handler.name
                ledger = EffectLedger(self.db, rc.run_id, task_id)
                ctx = StepContext(
                    run_id=rc.run_id,
                    task_id=task_id,
                    task_key=key,
                    attempt=attempt,
                    input=task_input,
                    dependencies=self._dependency_outputs(task_id),
                    project_root=self.project_root,
                    config=self.config,
                    policy=rc.policy,
                    cancel_event=rc.cancel_event,
                    effect=lambda k, action, probe=None, _l=ledger, _key=key: _l.apply(
                        f"{_key}:{k}", action, probe=probe
                    ),
                    save_artifact=self._artifact_saver(rc.run_id, task_id),
                    log=_log.bind(task=key, attempt=attempt, handler=handler.name),
                    budget=rc.budget,
                    record_usage=self._usage_recorder(rc, task_id),
                )
                try:
                    result = await asyncio.wait_for(handler.run(ctx), timeout=timeout)
                    _check_result(result, criteria)
                except asyncio.CancelledError:
                    self._finish_execution(execution_id, "cancelled", None)
                    self._set_task(task_id, status=TaskStatus.CANCELLED.value, finished_at=utcnow())
                    raise
                except ApprovalRequiredError as exc:
                    self._finish_execution(execution_id, "needs_approval", _err(exc))
                    self._set_task(task_id, status=TaskStatus.PENDING.value)
                    op = getattr(exc, "operation", None)
                    if op is not None:
                        self._request_task_approval(rc, task_id, op, str(exc))
                    return _TaskOutcome(PhaseOutcome.APPROVAL, op, f"task {key}: {exc}")
                except BudgetExceededError as exc:
                    self._finish_execution(execution_id, "budget", _err(exc))
                    self._set_task(task_id, status=TaskStatus.PENDING.value)
                    return _TaskOutcome(PhaseOutcome.BUDGET, reason=_err(exc))
                except Exception as exc:
                    error = _err(exc)
                    self._finish_execution(execution_id, "failed", error)
                    retryable = classify(exc) is ErrorClass.TRANSIENT and attempt < max_attempts
                    _log.warning(
                        "task attempt failed", task=key, attempt=attempt, error=error,
                        retry=retryable,
                    )  # fmt: skip
                    if retryable and not rc.cancel_event.is_set():
                        self._set_task(task_id, status=TaskStatus.PENDING.value, error=error)
                        await self.retry.sleep(attempt)
                        continue
                    self._fail_task(task_id, error)
                    return _TaskOutcome(PhaseOutcome.FAILED, reason=f"task {key}: {error}")
                self._succeed_task(rc, task_id, execution_id, result)
                return _TaskOutcome(PhaseOutcome.OK)

    def _usage_recorder(
        self, rc: _RunContext, task_id: uuid.UUID
    ) -> Callable[[ProviderName, str, TokenUsage, float], None]:
        """Persist LLM usage (``model_usage``) — the source of truth for budgets."""

        def record(provider: ProviderName, model: str, usage: TokenUsage, cost: float) -> None:
            with self.db.session() as session:
                session.add(
                    ModelUsage(
                        run_id=rc.run_id,
                        task_id=task_id,
                        provider=provider.value,
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=cost,
                    )
                )

        return record

    def _request_task_approval(
        self, rc: _RunContext, task_id: uuid.UUID, op: ApprovalOperation, reason: str
    ) -> None:
        with self.db.session() as session:
            run = self._get_run(session, rc.run_id)
            self.approvals.request(
                session, run, op, reason=reason, requested_by=self.actor, task_id=task_id
            )

    def _finish_execution(self, execution_id: uuid.UUID, status: str, error: str | None) -> None:
        with self.db.session() as session:
            execution = session.get(AgentExecution, execution_id)
            if execution is not None:
                execution.status = status
                execution.error = error
                execution.finished_at = utcnow()

    def _fail_task(self, task_id: uuid.UUID, error: str) -> None:
        self._set_task(task_id, status=TaskStatus.FAILED.value, error=error, finished_at=utcnow())

    def _succeed_task(
        self, rc: _RunContext, task_id: uuid.UUID, execution_id: uuid.UUID, result: StepResult
    ) -> None:
        output = _redact_json(result.output)
        with self.db.session() as session:
            task = session.get(Task, task_id)
            execution = session.get(AgentExecution, execution_id)
            run = self._get_run(session, rc.run_id)
            if task is None or execution is None:  # pragma: no cover
                raise RunNotFoundError("task or execution vanished")
            task.status = TaskStatus.SUCCEEDED.value
            task.output = output
            task.error = None
            task.finished_at = utcnow()
            execution.status = "succeeded"
            execution.output = output
            execution.evidence_refs = list(result.evidence_refs)
            execution.confidence = result.confidence
            execution.finished_at = utcnow()
            session.flush()
            self.checkpoints.save(session, run, f"task:{task.key}", task_id=task.id)


def _check_result(result: object, criteria: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(result, StepResult):
        raise SuccessCriteriaError("handler did not return a StepResult")
    output = result.output
    for schema in criteria:
        errors = sorted(
            Draft202012Validator(dict(schema)).iter_errors(dict(output)),
            key=lambda e: list(e.absolute_path),
        )
        if errors:
            where = "/".join(str(p) for p in errors[0].absolute_path) or "<root>"
            raise SuccessCriteriaError(
                f"success criteria not met at {where}: {redact(errors[0].message)}"
            )


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _redact_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_json(v) for v in value]
    return value


__all__ = [
    "Orchestrator",
    "PhaseOutcome",
    "PolicyDeniedError",
    "RunOptions",
    "RunStatus",
    "StepPreview",
    "local_actor",
    "short_id",
]
