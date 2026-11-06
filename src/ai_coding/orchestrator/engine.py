"""Orchestrator Engine: drives runs through the FSM and executes the task DAG.

Design (ADR-0006):

* All state lives in the database; the process holds only a *lease* on the run
  (renewed by a heartbeat). A crashed worker loses the lease and any worker can
  resume the run from the persisted state (AC-04, NFR-03).
* Each task attempt is recorded before it starts; success is recorded together
  with a checkpoint in one transaction. Side effects go through the effect ledger,
  so re-executing an interrupted task never repeats an applied effect.
* Run outcome is decided only from deterministic task statuses (ТЗ §5).

:class:`Orchestrator` is the facade; the read side (:mod:`.run_queries`), leases
(:mod:`.lease`), approvals of a run (:mod:`.run_approvals`) and what steps record
(:mod:`.recorders`) live in their own modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import socket
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import (
    ApprovalOperation,
    Mode,
    Outcome,
    Role,
    RunState,
    TaskPhase,
    TaskStatus,
)
from ai_coding.observability import bind_run, get_logger
from ai_coding.orchestrator.approval import ApprovalManager
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.budget import BudgetTracker
from ai_coding.orchestrator.checkpoint import CheckpointManager
from ai_coding.orchestrator.effects import EffectLedger
from ai_coding.orchestrator.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    ConcurrencyConflictError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NoRouteError,
    PolicyDeniedError,
    RunNotFoundError,
    SuccessCriteriaError,
)
from ai_coding.orchestrator.lease import LeaseKeeper
from ai_coding.orchestrator.planner import Plan
from ai_coding.orchestrator.policy import Decision, PolicyEngine, PolicyOptions
from ai_coding.orchestrator.recorders import TaskRecorders, redact_json
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.orchestrator.retry import ErrorClass, RetryPolicy, classify
from ai_coding.orchestrator.router import AgentRouter
from ai_coding.orchestrator.run_approvals import RunApprovals, role_of
from ai_coding.orchestrator.run_queries import RunQueries, RunStatus, get_run
from ai_coding.orchestrator.states import TERMINAL_STATES, StateManager
from ai_coding.persistence.artifacts import ArtifactStore, LocalArtifactStore
from ai_coding.persistence.db import Database
from ai_coding.persistence.models import (
    AgentExecution,
    Approval,
    ModelUsage,
    Project,
    ProjectMember,
    Run,
    RunEvent,
    Task,
    TaskDependency,
    User,
)
from ai_coding.persistence.s3 import S3ArtifactStore
from ai_coding.persistence.types import utcnow
from ai_coding.security import redact

_log = get_logger("ai_coding.orchestrator")
_FAILED_DEP = {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
# Run states that execute a phase of the DAG, and the state reached when it succeeds.
_PHASE_OF_STATE = {
    RunState.DISCOVERY: (TaskPhase.DISCOVERY, RunState.PLANNED),
    RunState.RUNNING: (TaskPhase.EXECUTION, RunState.VERIFYING),
    RunState.VERIFYING: (TaskPhase.VERIFICATION, RunState.COMPLETED),
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


# Which outcome of a phase's tasks decides the phase, strongest first.
_OUTCOME_PRECEDENCE = (
    PhaseOutcome.CANCELLED,
    PhaseOutcome.BUDGET,
    PhaseOutcome.APPROVAL,
    PhaseOutcome.FAILED,
)


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Per-run options (CLI flags)."""

    require_approval: bool = False
    # Caller data stored with the run (the server keeps the run kind and parameters
    # here so any worker can rebuild the handlers); not part of the plan digest.
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON form stored on the run."""
        return {**dict(self.extra), "require_approval": self.require_approval}


@dataclass(frozen=True, slots=True)
class StepPreview:
    """Dry-run view of a step: who would run it and what policy says."""

    key: str
    phase: TaskPhase
    capability: str
    handler: str | None
    route_error: str | None
    approval: Decision | None


@dataclass(slots=True)
class _TaskOutcome:
    kind: PhaseOutcome
    operation: ApprovalOperation | None = None
    reason: str = ""


@dataclass(slots=True)
class _RunContext:
    run_id: uuid.UUID
    project_id: uuid.UUID
    policy: PolicyEngine
    budget: BudgetTracker
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    lease_lost: bool = False

    def lose_lease(self) -> None:
        """Another worker took the run: stop, and tell running steps to stop."""
        self.lease_lost = True
        self.cancel_event.set()


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One attempt of a task, as recorded when it starts."""

    number: int
    key: str
    capability: str
    timeout: float | None
    max_attempts: int
    criteria: list[Mapping[str, Any]]
    input: dict[str, Any]
    budget_usd: float | None


def make_artifact_store(config: AICodingConfig, project_root: Path) -> ArtifactStore:
    """Local store under ``artifacts.path`` or, with ``store: minio``, an S3 bucket
    (credentials from ``AI_CODING_S3_*``; one prefix per project)."""
    if config.artifacts.store == "minio":
        return S3ArtifactStore.from_env(prefix=f"projects/{config.project.name}")
    root = Path(config.artifacts.path)
    return LocalArtifactStore(root if root.is_absolute() else project_root / root)


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
        local_owner: bool = True,
    ) -> None:
        self.db = db
        # Local mode: the single local user owns the project. The server passes False:
        # projects, users and roles are managed explicitly and never granted implicitly.
        self.local_owner = local_owner
        self.config = config
        self.project_root = project_root.resolve()
        self.registry = registry
        self.actor = actor or local_actor()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.retry = retry_policy or RetryPolicy()
        self.poll_interval = poll_interval
        self.audit = AuditLogger()
        self.states = StateManager(self.audit)
        self.approvals = ApprovalManager(self.audit)
        self.checkpoints = CheckpointManager()
        self.base_policy = PolicyEngine(config)
        self.artifacts = make_artifact_store(config, self.project_root)
        self.queries = RunQueries(db, self.approvals)
        self.leases = LeaseKeeper(db, self.worker_id, lease_ttl)
        self.run_approvals = RunApprovals(
            db, self.approvals, self.audit, self.base_policy, self.actor
        )
        self.recorders = TaskRecorders(db, self.artifacts)

    @property
    def lease_ttl(self) -> float:
        """Seconds a run lease lasts without renewal (the server worker sets its own)."""
        return self.leases.ttl

    @lease_ttl.setter
    def lease_ttl(self, seconds: float) -> None:
        self.leases.ttl = seconds

    def close(self) -> None:
        """Release database connections."""
        self.db.dispose()

    # ------------------------------------------------------------ project --
    def ensure_project(self, session: Session) -> Project:
        """Project row for this configuration; the local actor is its Owner.

        With ``local_owner=False`` (server) the project must exist and nothing is
        created or granted here.
        """
        project = session.scalar(select(Project).where(Project.name == self.config.project.name))
        if not self.local_owner:
            if project is None:
                raise RunNotFoundError(f"project '{self.config.project.name}' is not registered")
            return project
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
        return role_of(session, actor, project_id)

    # ----------------------------------------------------------- planning --
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
            self._add_tasks(session, run, plan)
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

    @staticmethod
    def _add_tasks(session: Session, run: Run, plan: Plan) -> None:
        """The plan's steps as PENDING tasks with their dependency edges."""
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
                requires_approval=step.requires_approval.value if step.requires_approval else None,
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

    # ----------------------------------------------------------- queries --
    def resolve_run_id(self, ref: str) -> uuid.UUID:
        """Full id from the full UUID, its :func:`short_id` or any unique prefix."""
        return self.queries.resolve_run_id(ref)

    def status(self, run_id: uuid.UUID) -> RunStatus:
        """Current snapshot of a run."""
        return self.queries.status(run_id)

    def task_output(self, run_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        """Stored (redacted) output of a succeeded task, or ``None``."""
        return self.queries.task_output(run_id, key)

    def task_input(self, run_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        """Input a task was planned with, or ``None`` if the run has no such task."""
        return self.queries.task_input(run_id, key)

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent runs of this project."""
        with self.db.session() as session:
            project = self.ensure_project(session)
            return RunQueries.list_runs(session, project.id, limit)

    def events(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        """Transition journal of a run."""
        return self.queries.events(run_id)

    def audit_trail(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        """Audit events of a run, oldest first (AC-10 export)."""
        return self.queries.audit_trail(run_id)

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
                    run = get_run(session, run_id)
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
        await asyncio.to_thread(self.leases.acquire, run_id)
        return await self._drive(run_id)

    async def resume(self, run_id: uuid.UUID, *, force: bool = False) -> RunStatus:
        """Continue a run after a crash, pause or approval (FR-09, AC-04)."""
        with self.db.session() as session:
            state = RunState(get_run(session, run_id).state)
            if state in TERMINAL_STATES:
                raise InvalidTransitionError(f"run {run_id} is {state} and cannot be resumed")
        await asyncio.to_thread(self.leases.acquire, run_id, force=force)
        previous: RunState | None = None
        with self.db.session() as session:
            run = get_run(session, run_id)
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
            run = get_run(session, run_id)
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
            self.run_approvals.expire_pending(session, run_id)
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
        return self.run_approvals.decide(
            run_id, approve=approve, approval_id=approval_id, note=note, actor=actor or self.actor
        )

    async def _drive(self, run_id: uuid.UUID) -> RunStatus:
        rc = self._run_context(run_id)
        heartbeat = asyncio.create_task(self.leases.heartbeat(run_id, rc.lose_lease))
        try:
            with bind_run(str(run_id), worker=self.worker_id):
                await self._loop(rc)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if not rc.lease_lost:
                await asyncio.to_thread(self.leases.release, run_id)
        return self.status(run_id)

    def _run_context(self, run_id: uuid.UUID) -> _RunContext:
        """Policy and budget of a run, the budget counting usage already recorded."""
        with self.db.session() as session:
            run = get_run(session, run_id)
            policy = self.run_approvals.policy_for(session, run)
            started = run.started_at or utcnow()
            project_id = run.project_id
            tokens, cost = session.execute(
                select(
                    func.coalesce(func.sum(ModelUsage.input_tokens + ModelUsage.output_tokens), 0),
                    func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
                ).where(ModelUsage.run_id == run_id)
            ).one()
        return _RunContext(
            run_id=run_id,
            project_id=project_id,
            policy=policy,
            budget=BudgetTracker(
                self.config.budgets, started_at=started, tokens=int(tokens), cost_usd=float(cost)
            ),
        )

    def _cancel_requested(self, run_id: uuid.UUID) -> bool:
        with self.db.session() as session:
            return bool(get_run(session, run_id).cancel_requested)

    def _state_of(self, run_id: uuid.UUID) -> RunState:
        with self.db.session() as session:
            return RunState(get_run(session, run_id).state)

    async def _loop(self, rc: _RunContext) -> None:
        """Advance the run state by state until it waits, pauses or ends (FSM)."""
        while not rc.lease_lost:
            if self._cancel_requested(rc.run_id):
                self._finalize_cancel(rc.run_id, "cancelled by user")
                return
            state = self._state_of(rc.run_id)
            if state in _PHASE_OF_STATE:
                phase, target = _PHASE_OF_STATE[state]
                proceed = await self._phase_then(rc, phase, target)
            elif state is RunState.CREATED:
                self._transition(rc.run_id, RunState.DISCOVERY)
                proceed = True
            elif state is RunState.PLANNED:
                proceed = self._after_plan(rc)
            elif state is RunState.AWAITING_APPROVAL:
                proceed = self._after_approval(rc)
            else:  # PAUSED or terminal
                return
            if not proceed:
                return

    def _after_plan(self, rc: _RunContext) -> bool:
        """PLANNED → RUNNING, or → AWAITING_APPROVAL if a step needs a human first."""
        if self.run_approvals.request_for_plan(rc.run_id, rc.policy):
            self._transition(rc.run_id, RunState.AWAITING_APPROVAL)
            return False
        self._transition(rc.run_id, RunState.RUNNING)
        return True

    def _after_approval(self, rc: _RunContext) -> bool:
        """AWAITING_APPROVAL → RUNNING with the granted operations, or the run ends."""
        decision = self.run_approvals.state(rc.run_id)
        if decision == "pending":
            return False
        if decision == "rejected":
            self._finalize_cancel(rc.run_id, "approval rejected or expired")
            return False
        with self.db.session() as session:
            rc.policy = self.run_approvals.policy_for(session, get_run(session, rc.run_id))
        self._transition(rc.run_id, RunState.RUNNING, reason="approved")
        return True

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

    async def _run_phase(self, rc: _RunContext, phase: TaskPhase) -> _TaskOutcome:
        """Run the phase's tasks in dependency order, ``max_parallel_tasks`` at a time."""
        self._retry_interrupted(rc.run_id, phase)
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
            skipped_any = not stop_launching and self._launch_ready(
                rc, phase, semaphore, running, awaiting_approval
            )
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
        return self._phase_result(rc.run_id, phase, outcomes)

    def _retry_interrupted(self, run_id: uuid.UUID, phase: TaskPhase) -> None:
        """A task left RUNNING belongs to an interrupted attempt (worker crash): retry it."""
        tasks, _, _ = self._load_graph(run_id)
        for task in tasks.values():
            if task.phase == phase.value and task.status == TaskStatus.RUNNING.value:
                self._set_task(task.id, status=TaskStatus.PENDING.value)

    def _launch_ready(
        self,
        rc: _RunContext,
        phase: TaskPhase,
        semaphore: asyncio.Semaphore,
        running: dict[asyncio.Task[_TaskOutcome], str],
        awaiting_approval: set[str],
    ) -> bool:
        """Start every pending task whose dependencies succeeded; skip those with a failed
        dependency. ``True`` if any was skipped (its dependents may be skipped next)."""
        tasks, _, deps = self._load_graph(rc.run_id)
        status = {k: TaskStatus(t.status) for k, t in tasks.items()}
        skipped_any = False
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
        return skipped_any

    def _phase_result(
        self, run_id: uuid.UUID, phase: TaskPhase, outcomes: Sequence[_TaskOutcome]
    ) -> _TaskOutcome:
        """The phase's outcome: the strongest task outcome, else failed tasks, else OK."""
        for kind in _OUTCOME_PRECEDENCE:
            for outcome in outcomes:
                if outcome.kind is kind:
                    return outcome
        # Tasks skipped because of failures in an earlier attempt also fail the phase.
        tasks, _, _ = self._load_graph(run_id)
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

    # --------------------------------------------------------------- task --
    def _dependency_outputs(self, task_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        with self.db.session() as session:
            rows = session.execute(
                select(Task.key, Task.output)
                .join(TaskDependency, TaskDependency.depends_on_id == Task.id)
                .where(TaskDependency.task_id == task_id)
            )
            return {key: dict(output or {}) for key, output in rows}

    def _begin_attempt(self, task_id: uuid.UUID) -> _Attempt:
        """Record the next attempt of a task as RUNNING (before anything executes)."""
        with self.db.session() as session:
            task = session.get(Task, task_id)
            if task is None:  # pragma: no cover
                raise RunNotFoundError(f"task {task_id} vanished")
            task.attempts += 1
            task.status = TaskStatus.RUNNING.value
            task.started_at = task.started_at or utcnow()
            return _Attempt(
                number=task.attempts,
                key=task.key,
                capability=task.capability,
                timeout=task.timeout_seconds,
                max_attempts=task.max_attempts,
                criteria=list(task.success_criteria or []),
                input=dict(task.input or {}),
                budget_usd=task.budget_usd,
            )

    def _start_execution(self, task_id: uuid.UUID, handler: str, attempt: int) -> uuid.UUID:
        """The ``agent_executions`` row of this attempt; the task records its handler."""
        with self.db.session() as session:
            execution = AgentExecution(
                task_id=task_id, agent_name=handler, attempt=attempt, status="running"
            )
            session.add(execution)
            session.flush()
            task = session.get(Task, task_id)
            if task is not None:
                task.handler = handler
            return execution.id

    def _step_context(
        self, rc: _RunContext, task_id: uuid.UUID, attempt: _Attempt, handler: str
    ) -> StepContext:
        ledger = EffectLedger(self.db, rc.run_id, task_id)
        key = attempt.key
        return StepContext(
            run_id=rc.run_id,
            task_id=task_id,
            task_key=key,
            attempt=attempt.number,
            input=attempt.input,
            dependencies=self._dependency_outputs(task_id),
            project_root=self.project_root,
            config=self.config,
            policy=rc.policy,
            cancel_event=rc.cancel_event,
            effect=lambda k, action, probe=None: ledger.apply(f"{key}:{k}", action, probe=probe),
            save_artifact=self.recorders.artifact_saver(rc.run_id, task_id),
            log=_log.bind(task=key, attempt=attempt.number, handler=handler),
            budget=rc.budget,
            record_usage=self.recorders.usage_recorder(rc.run_id, task_id),
            record_findings=self.recorders.findings_recorder(rc.run_id),
            project_id=rc.project_id,
        )

    async def _execute_task(
        self, rc: _RunContext, task_id: uuid.UUID, semaphore: asyncio.Semaphore
    ) -> _TaskOutcome:
        """Run a task until it succeeds, fails for good, pauses or is cancelled."""
        async with semaphore:
            while True:
                attempt = self._begin_attempt(task_id)
                try:
                    rc.budget.check()
                    route = AgentRouter(self.registry, rc.policy).route(
                        attempt.capability, max_cost_usd=attempt.budget_usd
                    )
                except BudgetExceededError as exc:
                    self._requeue(task_id, attempt)
                    return _TaskOutcome(PhaseOutcome.BUDGET, reason=_err(exc))
                except NoRouteError as exc:
                    self._fail_task(task_id, _err(exc))
                    return _TaskOutcome(
                        PhaseOutcome.FAILED, reason=f"task {attempt.key}: {_err(exc)}"
                    )
                handler = route.handler
                execution_id = self._start_execution(task_id, handler.name, attempt.number)
                ctx = self._step_context(rc, task_id, attempt, handler.name)
                try:
                    result = await asyncio.wait_for(handler.run(ctx), timeout=attempt.timeout)
                    _check_result(result, attempt.criteria)
                except asyncio.CancelledError:
                    self._finish_execution(execution_id, "cancelled", None)
                    self._set_task(task_id, status=TaskStatus.CANCELLED.value, finished_at=utcnow())
                    raise
                except ApprovalRequiredError as exc:
                    self._finish_execution(execution_id, "needs_approval", _err(exc))
                    self._requeue(task_id, attempt)  # a pause must not consume a retry
                    op = getattr(exc, "operation", None)
                    if op is not None:
                        self.run_approvals.request_for_task(rc.run_id, task_id, op, str(exc))
                    return _TaskOutcome(PhaseOutcome.APPROVAL, op, f"task {attempt.key}: {exc}")
                except BudgetExceededError as exc:
                    self._finish_execution(execution_id, "budget", _err(exc))
                    self._requeue(task_id, attempt)
                    return _TaskOutcome(PhaseOutcome.BUDGET, reason=_err(exc))
                except Exception as exc:
                    error = _err(exc)
                    self._finish_execution(execution_id, "failed", error)
                    if await self._retry_after(rc, task_id, attempt, exc, error):
                        continue
                    self._fail_task(task_id, error)
                    return _TaskOutcome(PhaseOutcome.FAILED, reason=f"task {attempt.key}: {error}")
                self._succeed_task(rc, task_id, execution_id, result)
                return _TaskOutcome(PhaseOutcome.OK)

    def _requeue(self, task_id: uuid.UUID, attempt: _Attempt) -> None:
        """Back to PENDING without counting the attempt (budget pause, approval)."""
        self._set_task(task_id, status=TaskStatus.PENDING.value, attempts=attempt.number - 1)

    async def _retry_after(
        self, rc: _RunContext, task_id: uuid.UUID, attempt: _Attempt, exc: Exception, error: str
    ) -> bool:
        """Whether a failed attempt is retried (transient error, attempts left, not
        cancelled); waits the back-off first."""
        retryable = classify(exc) is ErrorClass.TRANSIENT and attempt.number < attempt.max_attempts
        _log.warning("task attempt failed", task=attempt.key, attempt=attempt.number,
                     error=error, retry=retryable)  # fmt: skip
        if not retryable or rc.cancel_event.is_set():
            return False
        self._set_task(task_id, status=TaskStatus.PENDING.value, error=error)
        await self.retry.sleep(attempt.number)
        return True

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
        output = redact_json(result.output)
        with self.db.session() as session:
            task = session.get(Task, task_id)
            execution = session.get(AgentExecution, execution_id)
            run = get_run(session, rc.run_id)
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
