"""Project data lifecycle (S8, B-709, NFR-08): export, deletion, retention."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_coding.api.app import create_app
from ai_coding.api.lifecycle import delete_project, export_project, purge_expired
from ai_coding.api.services import ConflictError, ForbiddenError, NotFoundError, Principal, Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import (
    ApiToken,
    Artifact,
    AuditEvent,
    IdempotencyRecord,
    Run,
)
from ai_coding.persistence.types import utcnow

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    roots = tmp_path / "repos"
    for name in ("alpha", "beta"):
        (roots / name).mkdir(parents=True)
        (roots / name / "main.py").write_text("print(1)\n", encoding="utf-8")
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'server.db'}",
            allowed_roots=(roots.resolve(),),
        )
    )
    srv.migrate()
    yield srv
    srv.close()


@pytest.fixture
def owner(server: Server) -> Principal:
    server.create_user("owner", PASSWORD, is_admin=True)
    principal = server.login("owner", PASSWORD)
    assert principal is not None
    return principal


def project(server: Server, owner: Principal, name: str) -> uuid.UUID:
    return server.create_project(owner, name, path=str(server.settings.allowed_roots[0] / name))


async def test_export_contains_everything_and_only_this_project(
    server: Server, owner: Principal
) -> None:
    alpha, beta = project(server, owner, "alpha"), project(server, owner, "beta")
    run = server.create_run(owner, alpha, "discover", {})
    server.create_run(owner, beta, "discover", {})
    await Worker(server, poll_seconds=0.05).run_until_idle()
    archive = zipfile.ZipFile(io.BytesIO(export_project(server, owner, alpha)))
    names = set(archive.namelist())
    assert {
        "project.json",
        "runs.json",
        "tasks.json",
        "approvals.json",
        "run_events.json",
        "findings.json",
        "model_usage.json",
        "audit_events.json",
        "artifacts.json",
    } <= names
    meta = json.loads(archive.read("project.json"))
    assert meta["project"]["name"] == "alpha" and meta["exported_by"] == "owner"
    runs = json.loads(archive.read("runs.json"))
    assert [r["id"] for r in runs] == [str(run)]  # beta's run is not included
    events = json.loads(archive.read("audit_events.json"))
    assert events and {e["project_id"] for e in events} == {str(alpha)}
    files = [n for n in names if n.startswith("artifacts/")]
    assert files and archive.read(files[0])


def test_export_and_delete_need_the_owner(server: Server, owner: Principal) -> None:
    alpha = project(server, owner, "alpha")
    server.create_user("dev", PASSWORD)
    server.set_member(owner, alpha, "dev", Role.MAINTAINER)
    maint = server.login("dev", PASSWORD)
    assert maint is not None
    with pytest.raises(ForbiddenError):
        export_project(server, maint, alpha)
    with pytest.raises(ForbiddenError):
        delete_project(server, maint, alpha)


async def test_delete_keeps_the_audit_trail(server: Server, owner: Principal) -> None:
    alpha = project(server, owner, "alpha")
    active = server.create_run(owner, alpha, "discover", {})
    with pytest.raises(ConflictError, match="active runs"):
        delete_project(server, owner, alpha)
    await Worker(server, poll_seconds=0.05).run_until_idle()
    assert server.run_status(owner, active)["state"] == "COMPLETED"
    artifacts_root = server.settings.allowed_roots[0] / "alpha" / ".ai-coding" / "artifacts"
    assert any(artifacts_root.rglob("*"))
    delete_project(server, owner, alpha)
    with pytest.raises(NotFoundError):
        server.get_project(owner, alpha)
    assert not artifacts_root.exists()
    assert (server.settings.allowed_roots[0] / "alpha" / "main.py").is_file()  # user's files
    with server.session() as s:
        assert s.scalar(select(Run).where(Run.project_id == alpha)) is None
        actions = {
            e.action for e in s.scalars(select(AuditEvent).where(AuditEvent.project_id == alpha))
        }
    assert {"project.create", "run.create", "project.delete"} <= actions


async def test_retention_purges_old_runs_and_unreferenced_files(
    server: Server, owner: Principal
) -> None:
    alpha = project(server, owner, "alpha")
    old = server.create_run(owner, alpha, "discover", {})
    await Worker(server, poll_seconds=0.05).run_until_idle()
    with server.session() as s:
        row = s.get(Run, old)
        assert row is not None
        row.finished_at = utcnow() - timedelta(days=40)  # default retention: 30 days
        digests = set(s.scalars(select(Artifact.sha256).where(Artifact.run_id == old)))
        s.add(
            IdempotencyRecord(
                user_id=owner.user_id,
                key="k",
                method="POST",
                path="/x",
                body_sha256="0" * 64,
                status_code=201,
                response={},
                created_at=utcnow() - timedelta(days=2),
            )
        )
    fresh = server.create_run(owner, alpha, "discover", {})
    await Worker(server, poll_seconds=0.05).run_until_idle()
    token, token_id = server.issue_token(owner, "old")
    with server.session() as s:
        tok = s.get(ApiToken, token_id)
        assert tok is not None
        tok.expires_at = utcnow() - timedelta(days=60)
    counts = purge_expired(server)
    assert counts["runs"] == 1 and counts["idempotency_records"] == 1 and counts["tokens"] == 1
    with pytest.raises(NotFoundError):
        server.run_status(owner, old)
    assert server.run_status(owner, fresh)["state"] == "COMPLETED"
    root = server.settings.allowed_roots[0] / "alpha" / ".ai-coding" / "artifacts" / "sha256"
    with server.session() as s:
        kept = set(s.scalars(select(Artifact.sha256)))
    for digest in digests:
        assert (root / digest[:2] / digest).exists() == (digest in kept)
    assert purge_expired(server)["runs"] == 0  # idempotent
    assert server.authenticate(token) is None


def test_export_and_delete_over_http(server: Server, owner: Principal) -> None:
    alpha = project(server, owner, "alpha")
    token, _ = server.issue_token(owner, "t")
    h = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(server)) as client:
        exported = client.get(f"/api/v1/projects/{alpha}/export", headers=h)
        assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
        assert zipfile.ZipFile(io.BytesIO(exported.content)).read("project.json")
        assert client.delete(f"/api/v1/projects/{alpha}", headers=h).status_code == 204
        assert client.get(f"/api/v1/projects/{alpha}", headers=h).status_code == 404
