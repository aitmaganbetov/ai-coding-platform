"""Runs of the ``ai-coding`` CLI on developers' machines, reported to the server (ADR-0047).

The CLI works offline with its own SQLite database. Once its user signs in
(``ai-coding server login``) and links the project directory to a server project
(``ai-coding server link``), every run it finishes is reported here. Only the summary is
sent: state, verdict, gates, counts of new findings by severity, branch and commit. Code,
file paths and the findings themselves stay on the machine. A report is idempotent by the
run's id: the same run reported again (resumed, re-sent) updates its row.

Reporting needs ``run.create`` in the project (a member who may start runs); reading
needs ``run.view``. The machine name is what the CLI says, a label: the identity is the
token's user, and a run reported by one user cannot be overwritten by another.

Registered by :func:`ai_coding.api.app.create_app`; input is validated *after* the access
checks.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from ai_coding.api.app import API, Caller, server_of
from ai_coding.api.core import ConflictError, InvalidError, Principal, iso_or_none
from ai_coding.api.rbac import Permission
from ai_coding.domain.enums import Outcome
from ai_coding.persistence.models import LocalRun
from ai_coding.persistence.types import utcnow

if TYPE_CHECKING:
    from ai_coding.api.services import Server

router = APIRouter(prefix=API, tags=["local-runs"])
RECENT = timedelta(days=30)  # the machines the overview shows
MAX_LIST = 200
_NAME = r"^[A-Za-z0-9_.-]+$"


class GateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate: str = Field(min_length=1, max_length=64, pattern=_NAME)
    result: str = Field(min_length=1, max_length=16, pattern=_NAME)
    blocking: bool = False
    reason: str = Field(default="", max_length=300)


class LocalRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID
    machine: str = Field(min_length=1, max_length=255)
    cli_version: str = Field(min_length=1, max_length=40)
    mode: str = Field(min_length=1, max_length=20, pattern=_NAME)
    goal: str = Field(default="", max_length=200)
    state: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z_]+$")
    verdict: Literal["PASS", "FAIL"] | None = None
    branch: str | None = Field(default=None, max_length=255)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    gates: list[GateIn] = Field(default_factory=list, max_length=50)
    findings: dict[str, int] = Field(default_factory=dict, max_length=10)
    tasks: dict[str, int] = Field(default_factory=dict, max_length=10)
    error: str | None = Field(default=None, max_length=2000)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


def _parse(body: Any) -> LocalRunIn:
    try:
        data = LocalRunIn.model_validate(body)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidError(f"invalid local run report: {problems}") from None
    for name, counts in (("findings", data.findings), ("tasks", data.tasks)):
        if any(len(k) > 16 or not k.replace("_", "").isalnum() or v < 0 for k, v in counts.items()):
            raise InvalidError(f"invalid local run report: {name}: short names and counts >= 0")
    return data


def _dict(row: LocalRun) -> dict[str, Any]:
    return {
        "id": str(row.id), "run_id": str(row.run_id), "project_id": str(row.project_id),
        "reported_by": row.reported_by, "machine": row.machine, "cli_version": row.cli_version,
        "mode": row.mode, "goal": row.goal, "state": row.state, "verdict": row.verdict,
        "branch": row.branch, "commit": row.commit, "gates": row.gates,
        "findings": row.findings, "tasks": row.tasks, "error": row.error,
        "started_at": iso_or_none(row.started_at), "finished_at": iso_or_none(row.finished_at),
        "reported_at": iso_or_none(row.reported_at),
    }  # fmt: skip


def report(
    server: Server, principal: Principal, project_id: uuid.UUID, body: Any
) -> dict[str, Any]:
    """Record (or update) the summary of a CLI run; ``created`` tells which."""
    with server.session() as s:
        server.require(s, principal, project_id, Permission.RUN_CREATE)
        data = _parse(body)
        row = s.scalar(select(LocalRun).where(LocalRun.project_id == project_id,
                                              LocalRun.run_id == data.run_id))  # fmt: skip
        created = row is None
        if row is None:
            row = LocalRun(project_id=project_id, run_id=data.run_id,
                           reported_by=principal.username)  # fmt: skip
            s.add(row)
        elif row.reported_by != principal.username:
            raise ConflictError("this run was reported by another user")
        fields = data.model_dump(exclude={"run_id"})
        for name, value in fields.items():
            setattr(row, name, value)
        row.reported_at = utcnow()
        s.flush()
        server.record(s, principal.username, "local_run.report", Outcome.SUCCESS,
                      project_id=project_id, target=f"local-run:{data.run_id}",
                      details={"machine": data.machine, "state": data.state,
                               "verdict": data.verdict})  # fmt: skip
        return {**_dict(row), "created": created}


def project_runs(server: Server, principal: Principal, project_id: uuid.UUID,
                 limit: int = 50) -> list[dict[str, Any]]:  # fmt: skip
    """The project's CLI runs, latest report first."""
    with server.session() as s:
        server.require(s, principal, project_id, Permission.RUN_VIEW)
        rows = s.scalars(select(LocalRun).where(LocalRun.project_id == project_id)
                         .order_by(LocalRun.reported_at.desc())
                         .limit(max(1, min(limit, MAX_LIST))))  # fmt: skip
        return [_dict(r) for r in rows]


def overview(server: Server, principal: Principal) -> dict[str, Any]:
    """Connected CLIs in the caller's projects: each machine and user with its latest
    report of the last :data:`RECENT`, and how many reports came in the last day."""
    now = utcnow()
    with server.session() as s:
        names = server.visible_projects(s, principal, Permission.RUN_VIEW)
        if not names:
            return {"machines": [], "reports_24h": 0, "projects": 0, "last_report": None}
        visible = LocalRun.project_id.in_(list(names))
        rows = s.scalars(select(LocalRun).where(visible, LocalRun.reported_at >= now - RECENT)
                         .order_by(LocalRun.reported_at.desc()).limit(2000)).all()  # fmt: skip
        since = now - timedelta(days=1)
        day = s.scalar(select(func.count()).select_from(LocalRun)
                       .where(visible, LocalRun.reported_at >= since))  # fmt: skip
    machines: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r.machine, r.reported_by)
        if key in machines:
            machines[key]["reports"] += 1
            continue
        machines[key] = {"machine": r.machine, "user": r.reported_by, "reports": 1,
                         "project_id": str(r.project_id), "project": names.get(r.project_id),
                         "mode": r.mode, "state": r.state, "verdict": r.verdict,
                         "cli_version": r.cli_version,
                         "reported_at": iso_or_none(r.reported_at)}  # fmt: skip
    return {
        "machines": list(machines.values()),
        "reports_24h": int(day or 0),
        "projects": len({r.project_id for r in rows}),
        "last_report": iso_or_none(rows[0].reported_at) if rows else None,
    }


# ------------------------------------------------------------------ routes --
@router.post("/projects/{project_id}/local-runs")
async def post_local_run(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    body: dict[str, Any] = Body(...),  # noqa: B008 - FastAPI's way
) -> dict[str, Any]:
    """A run of the CLI on a developer's machine (its summary; idempotent by ``run_id``)."""
    return await run_in_threadpool(report, server_of(request), caller, project_id, body)


@router.get("/projects/{project_id}/local-runs")
async def get_local_runs(
    project_id: uuid.UUID, request: Request, caller: Caller,
    limit: int = Query(50, ge=1, le=MAX_LIST),
) -> list[dict[str, Any]]:  # fmt: skip
    """The project's CLI runs, latest report first."""
    return await run_in_threadpool(project_runs, server_of(request), caller, project_id, limit)


@router.get("/local-runs/overview")
async def local_runs_overview(request: Request, caller: Caller) -> dict[str, Any]:
    """Connected CLIs in the caller's projects (the infrastructure page)."""
    return await run_in_threadpool(overview, server_of(request), caller)


__all__ = ["LocalRunIn", "overview", "project_runs", "report", "router"]
