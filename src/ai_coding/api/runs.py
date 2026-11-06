"""Runs and their results (ТЗ §13): planning a run of a kind and queueing it for the
workers, status, approvals, cancellation, artifacts, findings and the audit trail."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, or_, select

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
    Project,
    ProjectMember,
    Run,
    RunJob,
)
from ai_coding.persistence.types import utcnow


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
    ) -> uuid.UUID:
        """Plan a run of ``kind`` and queue it for the workers."""
        spec = KINDS.get(kind)
        if spec is None:
            raise InvalidError(f"unknown run kind '{kind}' (known: {', '.join(sorted(KINDS))})")
        with self.session() as s:
            self.require(s, principal, project_id, spec.permission)
            project = detached_project(s, project_id)
        orch, config = self.orchestrator(project, actor=principal.username)
        try:
            (_registry, steps), fixed = build_kind(kind, orch.project_root, config, params)
        except (ValueError, KeyError) as exc:
            raise InvalidError(f"invalid parameters: {exc}") from exc
        try:
            run_id = orch.create_run(
                TaskPlanner().build(steps),
                mode=spec.mode,
                goal=_goal(kind, fixed),
                idempotency_key=idempotency_key,
                options=RunOptions(extra={"kind": kind, "params": fixed}),
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
        orch, _ = self.orchestrator(project, actor=principal.username)
        status = orch.status(run_id)
        return {**status.to_dict(), "project_id": str(project.id), "kind": kind, "jobs": jobs}

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
            return [_finding_dict(f) for f in rows]

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
                    if allowed(self._role(s, principal, m.project_id), Permission.AUDIT_VIEW)
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


def _finding_dict(f: Finding) -> dict[str, Any]:
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
