"""Runs and their results (ТЗ §13): planning a run of a kind and queueing it for the
workers, status, approvals, cancellation, artifacts, findings and the audit trail."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session, aliased

from ai_coding.api.core import (
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    detached_project,
    iso_or_none,
    str_or_none,
)
from ai_coding.api.kinds import KINDS, build_kind
from ai_coding.api.projects import ProjectService
from ai_coding.api.rbac import Permission, allowed
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import Outcome
from ai_coding.orchestrator import HandlerRegistry, Orchestrator, RunOptions, TaskPlanner
from ai_coding.orchestrator.errors import OrchestratorError
from ai_coding.persistence.models import (
    Artifact,
    AuditEvent,
    Finding,
    ModelUsage,
    Project,
    ProjectMember,
    Run,
    RunJob,
    Task,
    TaskDependency,
)
from ai_coding.persistence.types import utcnow

# Kinds that only read the tree they examine: they may examine a workspace snapshot.
SOURCE_KINDS = frozenset({"audit", "discover"})
GATES_TASK = "quality.gates"  # the audit step whose output holds the Quality Gates


class RunService(ProjectService):
    """Runs, approvals, cancellation, artifacts, findings and audit events."""

    def orchestrator(
        self, project: Project, registry: HandlerRegistry | None = None, *, actor: str
    ) -> tuple[Orchestrator, AICodingConfig]:
        root, config = self.project_config(project)
        orch = Orchestrator(
            db=self.db,
            config=config,
            project_root=root,
            registry=registry or HandlerRegistry([]),
            actor=actor,
            worker_id=f"{self.worker_id}:{uuid.uuid4().hex[:6]}",
            local_owner=False,
        )
        return orch, config

    def create_run(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        kind: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        source: Path | None = None,
        goal: str | None = None,
    ) -> uuid.UUID:
        """Plan a run of ``kind`` and queue it for the workers.

        ``source``: a workspace snapshot under :attr:`ServerSettings.sources_dir` the run
        examines instead of the project's tree (read-only kinds only). Set by the server
        (the chat agents' bridge, ADR-0038), never taken from request parameters.
        """
        spec = KINDS.get(kind)
        if spec is None:
            raise InvalidError(f"unknown run kind '{kind}' (known: {', '.join(sorted(KINDS))})")
        if source is not None and kind not in SOURCE_KINDS:
            raise InvalidError(f"a '{kind}' run works on the project, not on a workspace")
        with self.session() as s:
            self.require(s, principal, project_id, spec.permission)
            project = detached_project(s, project_id)
        if source is not None:
            source = self.run_source(project_id, str(source))
        orch, config = self.orchestrator(project, actor=principal.username)
        try:
            built = build_kind(kind, source or orch.project_root, config, params)
            (_registry, steps), fixed = built
        except (ValueError, KeyError) as exc:
            raise InvalidError(f"invalid parameters: {exc}") from exc
        extra: dict[str, Any] = {"kind": kind, "params": fixed}
        if source is not None:
            extra["source"] = str(source)
        try:
            run_id = orch.create_run(
                TaskPlanner().build(steps),
                mode=spec.mode,
                goal=(goal or _goal(kind, fixed))[:200],
                idempotency_key=idempotency_key,
                options=RunOptions(extra=extra),
            )
        except OrchestratorError as exc:
            raise ConflictError(str(exc)) from exc
        with self.session() as s:
            exists = s.scalar(select(RunJob).where(RunJob.run_id == run_id))
            if exists is None:
                s.add(RunJob(run_id=run_id, kind="execute", state="queued"))
            self.record(
                s,
                principal.username,
                "run.create",
                Outcome.SUCCESS,
                project_id=project_id,
                run_id=run_id,
                target=kind,
            )
        return run_id

    def run_source(self, project_id: uuid.UUID, source: str) -> Path:
        """The snapshot a run examines: an existing directory of this project's sources."""
        base = (self.settings.sources_dir / str(project_id)).resolve()
        path = Path(source).resolve()
        if not path.is_relative_to(base) or path == base or not path.is_dir():
            raise InvalidError("the run's source is not a snapshot of this project")
        return path

    def run_status(self, principal: Principal, run_id: uuid.UUID) -> dict[str, Any]:
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.RUN_VIEW)
            project = detached_project(s, run.project_id)
            jobs = [
                {"kind": j.kind, "state": j.state, "attempts": j.attempts, "error": j.error}
                for j in s.scalars(
                    select(RunJob).where(RunJob.run_id == run_id).order_by(RunJob.created_at)
                )
            ]
            kind = run.options.get("kind")
            details = _run_details(s, run_id)
        orch, _ = self.orchestrator(project, actor=principal.username)
        status = orch.status(run_id)
        return {**status.to_dict(), "project_id": str(project.id), "kind": kind, "jobs": jobs,
                **details}  # fmt: skip

    def list_runs(
        self, principal: Principal, project_id: uuid.UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
            rows = s.scalars(
                select(Run)
                .where(Run.project_id == project_id)
                .order_by(Run.created_at.desc())
                .limit(min(limit, 200))
            )
            return [
                {
                    "id": str(r.id),
                    "state": r.state,
                    "mode": r.mode,
                    "goal": r.goal,
                    "kind": r.options.get("kind"),
                    "requested_by": r.requested_by,
                    "created_at": iso_or_none(r.created_at),
                    "finished_at": iso_or_none(r.finished_at),
                }
                for r in rows
            ]

    def decide(
        self,
        principal: Principal,
        run_id: uuid.UUID,
        *,
        approve: bool,
        approval_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Approve or reject pending approvals, then queue the run to continue."""
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.RUN_APPROVE)
            project = detached_project(s, run.project_id)
        orch, _ = self.orchestrator(project, actor=principal.username)
        try:
            decided = orch.decide(
                run_id,
                approve=approve,
                approval_id=approval_id,
                note=note,
                actor=principal.username,
            )
        except OrchestratorError as exc:
            raise ConflictError(str(exc)) from exc
        status = orch.status(run_id)
        if not [a for a in status.approvals if a["status"] == "pending"]:
            self._enqueue(run_id, "resume")
        return {"decided": [str(a.id) for a in decided], "state": status.state.value}

    def cancel(self, principal: Principal, run_id: uuid.UUID, reason: str) -> dict[str, Any]:
        with self.session() as s:
            run, role = self._run(s, principal, run_id, Permission.RUN_CANCEL_OWN)
            if (
                not allowed(role, Permission.RUN_CANCEL_ANY)
                and run.requested_by != principal.username
            ):
                self._denied(
                    principal.username, "run.cancel", project_id=run.project_id, run_id=run_id
                )
                raise ForbiddenError("developers may cancel only their own runs")
            project = detached_project(s, run.project_id)
        orch, _ = self.orchestrator(project, actor=principal.username)
        try:
            status = orch.cancel(run_id, reason=reason[:500] or "cancelled via API")
        except OrchestratorError as exc:
            raise ConflictError(str(exc)) from exc
        with self.session() as s:
            for job in s.scalars(
                select(RunJob).where(RunJob.run_id == run_id, RunJob.state == "queued")
            ):
                job.state, job.finished_at = "done", utcnow()
        return {"state": status.state.value}

    def _enqueue(self, run_id: uuid.UUID, kind: str) -> None:
        with self.session() as s:
            busy = s.scalar(
                select(RunJob).where(
                    RunJob.run_id == run_id, RunJob.state.in_(("queued", "leased"))
                )
            )
            if busy is None:
                s.add(RunJob(run_id=run_id, kind=kind, state="queued"))

    def artifacts(self, principal: Principal, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with self.session() as s:
            self._run(s, principal, run_id, Permission.ARTIFACTS_VIEW)
            rows = s.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
            )
            return [
                {
                    "id": str(a.id),
                    "name": a.name,
                    "sha256": a.sha256,
                    "media_type": a.media_type,
                    "size_bytes": a.size_bytes,
                    "created_at": iso_or_none(a.created_at),
                }
                for a in rows
            ]

    def artifact_content(
        self, principal: Principal, run_id: uuid.UUID, artifact_id: uuid.UUID
    ) -> tuple[str, str, bytes]:
        """``(name, media type, bytes)`` of one artifact of the run."""
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.ARTIFACTS_VIEW)
            art = s.get(Artifact, artifact_id)
            if art is None or art.run_id != run_id:
                raise NotFoundError("artifact not found")
            project = detached_project(s, run.project_id)
            name, media, digest = art.name, art.media_type, art.sha256
        orch, _ = self.orchestrator(project, actor=principal.username)
        return name, media, orch.artifacts.get(digest)

    def findings(
        self, principal: Principal, project_id: uuid.UUID, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.FINDINGS_VIEW)
            rows = s.scalars(
                select(Finding)
                .where(Finding.project_id == project_id)
                .order_by(Finding.created_at.desc())
                .limit(min(limit, 2000))
            )
            return [finding_dict(f) for f in rows]

    def audit_events(
        self, principal: Principal, project_id: uuid.UUID | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Audit trail of one project, or of every project the caller may audit.

        Unscoped administrators also see server-level events (users, logins, probes of
        foreign projects), which belong to no project.
        """
        server_level = principal.is_admin and principal.token_project_id is None
        with self.session() as s:
            if project_id is not None:
                self.require(s, principal, project_id, Permission.AUDIT_VIEW)
                projects = [project_id]
            else:
                members = s.scalars(
                    select(ProjectMember).where(ProjectMember.user_id == principal.user_id)
                ).all()
                projects = [
                    m.project_id
                    for m in members
                    if allowed(self.role_in(s, principal, m.project_id), Permission.AUDIT_VIEW)
                ]
            scope: ColumnElement[bool] = AuditEvent.project_id.in_(projects)
            if project_id is None and server_level:
                scope = or_(scope, AuditEvent.project_id.is_(None))
            elif not projects:
                return []
            rows = s.scalars(
                select(AuditEvent)
                .where(scope)
                .order_by(AuditEvent.created_at.desc())
                .limit(min(limit, 1000))
            )
            return [
                {
                    "id": str(e.id),
                    "created_at": iso_or_none(e.created_at),
                    "actor": e.actor,
                    "actor_role": e.actor_role,
                    "project_id": str_or_none(e.project_id),
                    "run_id": str_or_none(e.run_id),
                    "action": e.action,
                    "target": e.target,
                    "outcome": e.outcome,
                    "details": e.details,
                }
                for e in rows
            ]


def _goal(kind: str, params: dict[str, Any]) -> str:
    title = params.get("title")
    return f"{kind}: {title}"[:200] if title else kind


# What a finished step reports in a run's ``results`` (the agents' workspace shows them):
# where the change is (branch, base, commit, pull request) and the agents' verdict. Only
# these scalar fields, clipped: a step's full output stays in its artifacts.
RESULT_FIELDS = ("branch", "base", "commit", "sha", "url", "number", "iterations", "verdict",
                 "summary")  # fmt: skip
RESULT_CHARS = 2000


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _results(s: Session, run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Per succeeded step, the :data:`RESULT_FIELDS` of its output."""
    results: dict[str, dict[str, Any]] = {}
    for key, output in s.execute(
        select(Task.key, Task.output).where(Task.run_id == run_id, Task.status == "SUCCEEDED")
    ):
        if not isinstance(output, dict):
            continue
        picked = {
            name: value[:RESULT_CHARS] if isinstance(value, str) else value
            for name in RESULT_FIELDS
            if _scalar(value := output.get(name))
        }
        if picked:
            results[key] = picked
    return results


def _run_details(s: Session, run_id: uuid.UUID) -> dict[str, Any]:
    """What the run page draws besides the status: the edges of the task graph, the
    Quality Gates (``None`` until the gates step has an output), the LLM usage and what
    the finished steps report (:func:`_results`)."""
    task, needed = aliased(Task), aliased(Task)
    edges = s.execute(
        select(task.key, needed.key)
        .select_from(TaskDependency)
        .join(task, task.id == TaskDependency.task_id)
        .join(needed, needed.id == TaskDependency.depends_on_id)
        .where(task.run_id == run_id)
        .order_by(task.key, needed.key)
    ).all()
    output = s.scalar(select(Task.output).where(Task.run_id == run_id, Task.key == GATES_TASK))
    gates = output.get("gates") if isinstance(output, dict) else None
    usage = s.execute(select(*usage_sums()).where(ModelUsage.run_id == run_id)).one()
    return {
        "dependencies": [{"task": key, "depends_on": dep} for key, dep in edges],
        "gates": gates if isinstance(gates, list) else None,
        "usage": usage_dict(*usage),
        "results": _results(s, run_id),
    }


def usage_sums() -> tuple[ColumnElement[Any], ColumnElement[Any], ColumnElement[Any]]:
    """Input tokens, output tokens and cost of the selected ``model_usage`` rows (zero
    when there are none)."""
    return (
        func.coalesce(func.sum(ModelUsage.input_tokens), 0),
        func.coalesce(func.sum(ModelUsage.output_tokens), 0),
        func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
    )


def usage_dict(input_tokens: Any, output_tokens: Any, cost_usd: Any) -> dict[str, Any]:
    """JSON form of :func:`usage_sums` (PostgreSQL sums integers as ``Decimal``)."""
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cost_usd": round(float(cost_usd or 0.0), 6),
    }


def finding_dict(f: Finding) -> dict[str, Any]:
    return {
        c.key: (
            str(v)
            if isinstance(v := getattr(f, c.key), uuid.UUID)
            else iso_or_none(v)
            if hasattr(v, "isoformat")
            else v
        )
        for c in Finding.__table__.columns
    }
