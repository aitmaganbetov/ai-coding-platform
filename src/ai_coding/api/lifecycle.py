"""Project data lifecycle (NFR-08, B-709): export, deletion, retention purge.

* **Export** (owner): a ZIP with the project's metadata, members, runs with tasks,
  approvals and state journal, findings, model usage, its audit trail and the
  artifact files — everything the platform stores about the project.
* **Deletion** (owner): refused while runs are active; removes the project's rows
  (cascade), its artifacts and the server-managed checkout. The audit trail is
  append-only and keeps the record of what happened, including the deletion.
* **Retention**: finished runs older than the retention period (server setting, else
  ``artifacts.retention_days`` of the project) are purged with their artifacts;
  expired idempotency records and long-dead tokens are removed.
"""

from __future__ import annotations

import io
import json
import shutil
import uuid
import zipfile
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ai_coding.api.rbac import Permission
from ai_coding.api.services import ConflictError, InvalidError, Principal, Server
from ai_coding.domain.enums import Outcome
from ai_coding.orchestrator.states import TERMINAL_STATES
from ai_coding.persistence.models import (
    ApiToken,
    Approval,
    Artifact,
    AuditEvent,
    Base,
    Finding,
    IdempotencyRecord,
    ModelUsage,
    Project,
    ProjectMember,
    Run,
    RunEvent,
    Task,
    User,
)
from ai_coding.persistence.types import utcnow

EXPORT_FORMAT = 1
IDEMPOTENCY_TTL = timedelta(days=1)
DEAD_TOKEN_TTL = timedelta(days=30)
_TERMINAL = [s.value for s in TERMINAL_STATES]


def _row(obj: Base) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.key)
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        out[column.key] = value
    return out


def _dump(zf: zipfile.ZipFile, name: str, data: Any) -> None:
    zf.writestr(name, json.dumps(data, indent=2, ensure_ascii=False, default=str))


def export_project(server: Server, principal: Principal, project_id: uuid.UUID) -> bytes:
    """ZIP archive of all data the platform holds about the project."""
    with server.session() as s:
        server.require(s, principal, project_id, Permission.PROJECT_EXPORT)
        project = s.get(Project, project_id)
        if project is None:  # pragma: no cover - require() checked it
            raise InvalidError("project not found")
        run_ids = list(s.scalars(select(Run.id).where(Run.project_id == project_id)))
        members = s.execute(
            select(User.username, ProjectMember.role)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project_id)
        ).all()
        data = {
            "project.json": {
                "format": EXPORT_FORMAT,
                "exported_at": utcnow().isoformat(),
                "exported_by": principal.username,
                "project": _row(project),
                "members": [{"username": u, "role": r} for u, r in members],
            },
            "runs.json": [_row(r) for r in s.scalars(select(Run).where(Run.id.in_(run_ids)))],
            "tasks.json": [
                _row(t) for t in s.scalars(select(Task).where(Task.run_id.in_(run_ids)))
            ],
            "approvals.json": [
                _row(a) for a in s.scalars(select(Approval).where(Approval.run_id.in_(run_ids)))
            ],
            "run_events.json": [
                _row(e) for e in s.scalars(select(RunEvent).where(RunEvent.run_id.in_(run_ids)))
            ],
            "findings.json": [
                _row(f) for f in s.scalars(select(Finding).where(Finding.project_id == project_id))
            ],
            "model_usage.json": [
                _row(m) for m in s.scalars(select(ModelUsage).where(ModelUsage.run_id.in_(run_ids)))
            ],
            "audit_events.json": [
                _row(e)
                for e in s.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.project_id == project_id)
                    .order_by(AuditEvent.created_at)
                )
            ],
        }
        artifacts = list(s.scalars(select(Artifact).where(Artifact.run_id.in_(run_ids))))
        s.expunge(project)
        server.record(
            s,
            principal.username,
            "project.export",
            Outcome.SUCCESS,
            project_id=project_id,
            details={"runs": len(run_ids)},
        )
    orch, _ = server.orchestrator(project, actor=principal.username)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, value in data.items():
            _dump(zf, name, value)
        _dump(zf, "artifacts.json", [_row(a) for a in artifacts])
        for art in artifacts:
            try:
                content = orch.artifacts.get(art.sha256)
            except (OSError, ValueError):
                continue  # already purged; the manifest still lists it
            zf.writestr(f"artifacts/{art.sha256[:12]}-{_safe(art.name)}", content)
    return buffer.getvalue()


def delete_project(server: Server, principal: Principal, project_id: uuid.UUID) -> None:
    """Delete the project and its data (owner); the audit trail is kept."""
    with server.session() as s:
        server.require(s, principal, project_id, Permission.PROJECT_DELETE)
        active = s.scalar(
            select(Run.id).where(Run.project_id == project_id, Run.state.not_in(_TERMINAL)).limit(1)
        )
        if active is not None:
            raise ConflictError("cancel or finish the project's active runs first")
        project = s.get(Project, project_id)
        if project is None:  # pragma: no cover - require() checked it
            raise InvalidError("project not found")
        s.expunge(project)
    orch, _ = server.orchestrator(project, actor=principal.username)
    artifacts_root = orch.artifacts.root
    root = orch.project_root
    with server.session() as s:
        s.execute(delete(Project).where(Project.id == project_id))
        server.record(
            s,
            principal.username,
            "project.delete",
            Outcome.SUCCESS,
            project_id=project_id,
            target=project.name,
        )
    shutil.rmtree(artifacts_root, ignore_errors=True)
    managed = server.settings.projects_dir.resolve()
    if root.resolve().is_relative_to(managed):  # a clone the server made: remove it
        shutil.rmtree(root, ignore_errors=True)


def purge_expired(server: Server, *, now: Any = None) -> dict[str, int]:
    """Apply retention; counts of removed items."""
    now = now or utcnow()
    counts = {"runs": 0, "artifact_files": 0, "idempotency_records": 0, "tokens": 0}
    with server.session() as s:
        projects = list(s.scalars(select(Project)))
        for p in projects:
            s.expunge(p)
    for project in projects:
        removed_runs, removed_files = _purge_project(server, project, now)
        counts["runs"] += removed_runs
        counts["artifact_files"] += removed_files
    with server.session() as s:
        counts["idempotency_records"] = _count(
            s.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.created_at < now - IDEMPOTENCY_TTL
                )
            )
        )
        dead = (ApiToken.revoked_at < now - DEAD_TOKEN_TTL) | (
            ApiToken.expires_at < now - DEAD_TOKEN_TTL
        )
        counts["tokens"] = _count(s.execute(delete(ApiToken).where(dead)))
    return counts


def _purge_project(server: Server, project: Project, now: Any) -> tuple[int, int]:
    try:
        orch, config = server.orchestrator(project, actor="retention")
    except InvalidError:
        return 0, 0  # unreadable configuration: never guess a retention period
    days = server.settings.retention_days or config.artifacts.retention_days
    cutoff = now - timedelta(days=days)
    with server.session() as s:
        old = list(
            s.scalars(
                select(Run.id).where(
                    Run.project_id == project.id,
                    Run.state.in_(_TERMINAL),
                    Run.finished_at.is_not(None),
                    Run.finished_at < cutoff,
                )
            )
        )
        if not old:
            return 0, 0
        digests = set(s.scalars(select(Artifact.sha256).where(Artifact.run_id.in_(old))))
        s.execute(delete(Run).where(Run.id.in_(old)))
        s.flush()
        kept = _project_digests(s, project.id)
        server.record(
            s,
            "retention",
            "retention.purge",
            Outcome.SUCCESS,
            project_id=project.id,
            details={"runs": len(old), "days": days},
        )
    files = 0
    for digest in digests - kept:
        path = orch.artifacts.root / "sha256" / digest[:2] / digest
        if path.is_file():
            path.unlink()
            files += 1
    return len(old), files


def _project_digests(s: Session, project_id: uuid.UUID) -> set[str]:
    return set(
        s.scalars(
            select(Artifact.sha256)
            .join(Run, Run.id == Artifact.run_id)
            .where(Run.project_id == project_id)
        )
    )


def _count(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:100] or "artifact"
