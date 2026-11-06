"""Read-only views for the Web UI: the agent catalogue, runs and findings across the
caller's projects, a project's agent executions and LLM usage, and the instance status.

Nothing here changes data. The cross-project lists hold only the projects in which the
caller's effective role (capped by the token) grants the permission — the rule of the
per-project endpoints (:meth:`~ai_coding.api.core.ServerCore.require`), so an
administrator who is not a member sees no project data here either. A filter naming a
project the caller cannot see matches nothing instead of revealing that it exists.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ai_coding import __version__
from ai_coding.agents.catalog import CATALOG
from ai_coding.api import presence
from ai_coding.api.accounts import AccountService
from ai_coding.api.core import Principal, iso_or_none
from ai_coding.api.rbac import Permission, allowed, capped
from ai_coding.api.runs import RunService, finding_dict, usage_dict, usage_sums
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import (
    AgentExecution,
    Finding,
    ModelUsage,
    Project,
    ProjectMember,
    Run,
    RunJob,
    Task,
)
from ai_coding.persistence.types import utcnow
from ai_coding.tools.prepare import is_prepared
from ai_coding.tools.registry import builtin_tools

MAX_RUNS = 200
MAX_FINDINGS = 2000
MAX_EXECUTIONS = 500
ERROR_CHARS = 500
# A worker shows in the status while it holds a job lease, and for this long after its
# last lease expired (it stopped renewing: crashed, stalled or cut off from the database).
WORKER_SEEN = timedelta(minutes=10)
# Analysers whose offline vulnerability databases ``ai-coding tools pull`` prepares.
ANALYSERS = ("trivy", "osv-scanner")


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _workers(beats: list[dict[str, Any]], leases: Any, now: datetime) -> list[dict[str, Any]]:
    """The workers of :meth:`InsightService.system_status`: heartbeats and job leases
    (``lease_owner, live leases, latest lease end``) merged by worker id."""
    merged: dict[str, dict[str, Any]] = {
        b["id"]: {"id": b["id"], "online": True, "active_jobs": 0, "last_seen": b["seen"],
                  "host": b["host"], "started": b["started"], "concurrency": b["concurrency"]}
        for b in beats
    }  # fmt: skip
    for owner, active, last in leases:
        worker = merged.setdefault(owner, {"id": owner, "online": False, "active_jobs": 0,
                                           "last_seen": None, "host": None, "started": None,
                                           "concurrency": None})  # fmt: skip
        worker["active_jobs"] = int(active or 0)
        worker["online"] = worker["online"] or worker["active_jobs"] > 0
        if last is not None:
            ended = min(last, now)
            worker["last_seen"] = max(worker["last_seen"] or ended, ended)
    return [
        {**w, "last_seen": iso_or_none(w["last_seen"]), "started": iso_or_none(w["started"])}
        for w in sorted(merged.values(), key=lambda w: str(w["id"]))
    ]


class InsightService(AccountService, RunService):
    """The agent catalogue, cross-project lists, agent executions, usage, system status."""

    # ----------------------------------------------------------- catalogue --
    @staticmethod
    def agents() -> list[dict[str, Any]]:
        """The twelve agents of ТЗ §6.

        ``capabilities``: the registry capability an agent runs under as a step;
        ``allowed_tools``: the tools its contract grants — none (``AgentTask``), agents
        only answer with structured output that the orchestrator validates and applies.
        """
        return [
            {
                "name": spec.name.value,
                "inputs": spec.inputs,
                "outputs": spec.outputs,
                "requires_llm": spec.requires_llm,
                "implemented": spec.implemented,
                "sprint": spec.sprint,
                "capabilities": [f"agent.{spec.name.value}"],
                "allowed_tools": [],
            }
            for spec in CATALOG
        ]

    # -------------------------------------------------------- cross-project --
    def visible_projects(
        self, s: Session, principal: Principal, permission: Permission
    ) -> dict[uuid.UUID, str]:
        """Names of the live projects in which the caller's role grants ``permission``."""
        rows = s.execute(
            select(Project.id, Project.name, ProjectMember.role)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == principal.user_id, Project.deleted_at.is_(None))
        ).all()
        return {
            pid: name
            for pid, name, role in rows
            if principal.token_project_id in (None, pid)
            and allowed(capped(Role(role), principal.token_max_role), permission)
        }

    def _scope(
        self, s: Session, principal: Principal, permission: Permission, project_id: str | None
    ) -> dict[uuid.UUID, str]:
        names = self.visible_projects(s, principal, permission)
        if not project_id:
            return names
        wanted = _uuid_or_none(project_id)
        return {wanted: names[wanted]} if wanted in names else {}

    def all_runs(
        self,
        principal: Principal,
        *,
        limit: int = 50,
        state: str | None = None,
        kind: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Runs of every project the caller may view, newest first."""
        with self.session() as s:
            names = self._scope(s, principal, Permission.RUN_VIEW, project_id)
            if not names:
                return []
            query = select(Run).where(Run.project_id.in_(names))
            if state:
                query = query.where(Run.state == state)
            if kind:
                query = query.where(Run.options["kind"].as_string() == kind)
            rows = s.scalars(
                query.order_by(Run.created_at.desc(), Run.id.desc()).limit(min(limit, MAX_RUNS))
            )
            return [
                {
                    "id": str(r.id),
                    "project_id": str(r.project_id),
                    "project_name": names[r.project_id],
                    "state": r.state,
                    "mode": r.mode,
                    "kind": r.options.get("kind"),
                    "goal": r.goal,
                    "requested_by": r.requested_by,
                    "created_at": iso_or_none(r.created_at),
                    "finished_at": iso_or_none(r.finished_at),
                }
                for r in rows
            ]

    def all_findings(
        self,
        principal: Principal,
        *,
        limit: int = 500,
        severity: str | None = None,
        status: str | None = None,
        source: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Findings of every project the caller may view, newest first."""
        with self.session() as s:
            names = self._scope(s, principal, Permission.FINDINGS_VIEW, project_id)
            if not names:
                return []
            query = select(Finding).where(Finding.project_id.in_(names))
            for column, value in ((Finding.severity, severity), (Finding.status, status),
                                  (Finding.source, source)):  # fmt: skip
                if value:
                    query = query.where(column == value)
            if run_id:
                query = query.where(Finding.run_id == _uuid_or_none(run_id))
            rows = s.scalars(
                query.order_by(Finding.created_at.desc(), Finding.id.desc())
                .limit(min(limit, MAX_FINDINGS))
            )  # fmt: skip
            return [{**finding_dict(f), "project_name": names[f.project_id]} for f in rows]

    # --------------------------------------------------------- per project --
    def agent_executions(
        self, principal: Principal, project_id: uuid.UUID, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Attempts of the project's tasks by agents and handlers, newest first, with the
        tokens, cost and models of their task's LLM calls."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
            rows = s.execute(
                select(AgentExecution, Task.run_id, Task.key)
                .join(Task, Task.id == AgentExecution.task_id)
                .join(Run, Run.id == Task.run_id)
                .where(Run.project_id == project_id)
                .order_by(AgentExecution.started_at.desc(), AgentExecution.id.desc())
                .limit(min(limit, MAX_EXECUTIONS))
            ).all()
            tasks = {execution.task_id for execution, _run, _key in rows}
            sums = {
                task_id: usage_dict(*totals)
                for task_id, *totals in s.execute(
                    select(ModelUsage.task_id, *usage_sums())
                    .where(ModelUsage.task_id.in_(tasks))
                    .group_by(ModelUsage.task_id)
                )
            }
            models: dict[uuid.UUID | None, set[str]] = defaultdict(set)
            for task_id, provider, model in s.execute(
                select(ModelUsage.task_id, ModelUsage.provider, ModelUsage.model)
                .where(ModelUsage.task_id.in_(tasks))
                .distinct()
            ):
                models[task_id].add(f"{provider}/{model}")
        none = usage_dict(0, 0, 0.0)
        return [
            {
                "id": str(e.id),
                "run_id": str(run_id),
                "task_key": key,
                "agent_name": e.agent_name,
                "attempt": e.attempt,
                "status": e.status,
                "confidence": e.confidence,
                "error": e.error[:ERROR_CHARS] if e.error else e.error,
                "started_at": iso_or_none(e.started_at),
                "finished_at": iso_or_none(e.finished_at),
                "duration_seconds": (
                    round((e.finished_at - e.started_at).total_seconds(), 3)
                    if e.finished_at is not None
                    else None
                ),
                **sums.get(e.task_id, none),
                "models": sorted(models.get(e.task_id, ())),
            }
            for e, run_id, key in rows
        ]

    def usage(
        self, principal: Principal, project_id: uuid.UUID, *, days: int = 30
    ) -> dict[str, Any]:
        """LLM calls of the project's runs over the last ``days``: totals, per model and
        per UTC day (days without calls are left out)."""
        since = utcnow() - timedelta(days=days)
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
            scope = (Run.project_id == project_id, ModelUsage.created_at >= since)
            by_model = s.execute(
                select(ModelUsage.provider, ModelUsage.model, func.count(), *usage_sums())
                .join(Run, Run.id == ModelUsage.run_id)
                .where(*scope)
                .group_by(ModelUsage.provider, ModelUsage.model)
                .order_by(ModelUsage.provider, ModelUsage.model)
            ).all()
            # Days in Python: truncating a timestamp to its UTC date has no portable SQL.
            calls = s.execute(
                select(ModelUsage.created_at, ModelUsage.input_tokens, ModelUsage.output_tokens,
                       ModelUsage.cost_usd)
                .join(Run, Run.id == ModelUsage.run_id)
                .where(*scope)
            ).all()  # fmt: skip
        daily: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
        for created, tokens_in, tokens_out, cost in calls:
            day = daily[created.date().isoformat()]
            day[0] += tokens_in or 0
            day[1] += tokens_out or 0
            day[2] += cost or 0.0
        models = [
            {"provider": provider, "model": model, "calls": int(n), **usage_dict(*totals)}
            for provider, model, n, *totals in by_model
        ]
        return {
            "totals": {
                "calls": sum(m["calls"] for m in models),
                **usage_dict(*(sum(m[k] for m in models)
                               for k in ("input_tokens", "output_tokens", "cost_usd"))),
            },
            "by_model": models,
            "daily": [{"date": date, **usage_dict(*sums)} for date, sums in sorted(daily.items())],
        }  # fmt: skip

    # --------------------------------------------------------------- admin --
    def system_status(self, principal: Principal, *, workspaces: bool) -> dict[str, Any]:
        """Version, database, queue and runs by state, workers and the analysers' offline
        data (administrators). ``workspaces``: whether this API process serves terminals.

        Workers come from their heartbeats (:mod:`ai_coding.api.presence`), idle ones
        included, and from the job leases: ``active_jobs`` counts live leases,
        ``last_seen`` is the latest heartbeat or lease end. ``online``: a fresh heartbeat or
        a live lease; a worker seen only through an older lease (it stopped, or cannot
        write the data directory) is listed offline for :data:`WORKER_SEEN`.
        """
        self.require_admin(principal, "view the system status")
        now = utcnow()
        live = case((RunJob.lease_expires_at > now, 1), else_=0)
        with self.session() as s:
            queue = s.execute(select(RunJob.state, func.count()).group_by(RunJob.state)).all()
            runs = s.execute(select(Run.state, func.count()).group_by(Run.state)).all()
            leases = s.execute(
                select(RunJob.lease_owner, func.sum(live), func.max(RunJob.lease_expires_at))
                .where(RunJob.state == "leased", RunJob.lease_owner.is_not(None),
                       RunJob.lease_expires_at >= now - WORKER_SEEN)
                .group_by(RunJob.lease_owner)
                .order_by(RunJob.lease_owner)
            ).all()  # fmt: skip
        tools = {a.spec.name: a for a in builtin_tools()}
        return {
            "version": __version__,
            "database": self.db.engine.dialect.name,
            "workspaces": "on" if workspaces else "off",
            "queue": {state: int(n) for state, n in sorted(queue)},
            "runs": {state: int(n) for state, n in sorted(runs)},
            "workers": _workers(presence.alive(self.settings.data_dir, now), leases, now),
            "analysers": {name: name in tools and is_prepared(tools[name]) for name in ANALYSERS},
            "time": now.isoformat(),
        }  # fmt: skip
