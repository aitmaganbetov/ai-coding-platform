"""Opt-in API, tenant isolation, safe findings and pagination against a real database."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.services import NotFoundError, Server
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import Role, Severity
from ai_coding.persistence.models import Finding, Run, Task

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
SECRET = "ghp_" + "a" * 36


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
            allowed_roots=(repo.resolve(), other_repo.resolve()),
            rate_limit_per_minute=100_000,
        )
    )
    srv.migrate()
    for name in ("owner", "auditor", "outsider", "admin"):
        srv.create_user(name, PASSWORD, is_admin=name == "admin")
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    project = srv.create_project(owner, "project", path=str(repo))
    other = srv.create_project(owner, "other", path=str(other_repo))
    srv.set_member(owner, project, "auditor", Role.AUDITOR)
    with srv.session() as s:
        run = Run(project_id=project, mode="AUDIT", state="COMPLETED", requested_by="owner")
        s.add(run)
        s.flush()
        run_id = run.id
        s.add(
            Task(
                run_id=run_id,
                key="tool.semgrep",
                name="Semgrep",
                phase="EXECUTION",
                capability="scan.semgrep",
                status="SUCCEEDED",
                output={"tool": "semgrep", "succeeded": True},
            )
        )
        s.add(
            Task(
                run_id=run_id,
                key="tool.trivy",
                name="Trivy",
                phase="EXECUTION",
                capability="scan.trivy",
                status="RUNNING",
                output={"tool": "trivy", "succeeded": True},
            )
        )
        for source in ("gitleaks", "semgrep"):
            s.add(
                Finding(
                    project_id=project,
                    run_id=run_id,
                    source=source,
                    rule_id="rule",
                    severity=Severity.HIGH.value,
                    title=SECRET,
                    message=SECRET,
                    file_path="app.py",
                    line=1,
                    fingerprint=source.ljust(64, "0"),
                    evidence={"secret": SECRET},
                )
            )
        s.add(
            Finding(
                project_id=project,
                source="semgrep",
                severity=Severity.CRITICAL.value,
                title="different run",
                fingerprint="x" * 64,
            )
        )
    token, _ = srv.issue_token(owner, "test")
    yield {
        "srv": srv,
        "owner": owner,
        "project": project,
        "other": other,
        "run": run_id,
        "token": token,
    }
    srv.close()


def test_default_disabled(world: dict[str, Any]) -> None:
    with TestClient(create_app(world["srv"], broker=None)) as client:
        assert "security-center" not in client.get("/api/v1/openapi.json").text
        assert client.get(_url(world)).status_code == 404


def _url(world: dict[str, Any]) -> str:
    return f"/api/v1/projects/{world['project']}/security-center/runs/{world['run']}"


def test_snapshot_and_pagination(world: dict[str, Any]) -> None:
    srv = world["srv"]
    with TestClient(create_app(srv, broker=None, security_center_enabled=True)) as client:
        url = _url(world)
        assert client.get(url).status_code == 401
        client.headers["Authorization"] = f"Bearer {world['token']}"
        first = client.get(url, params={"limit": 1})
        assert first.status_code == 200
        data = first.json()
        assert SECRET not in first.text
        assert "evidence" not in first.text and "message" not in first.text
        assert data["summary"]["total"] == 2
        assert data["summary"]["by_severity"][Severity.CRITICAL.value] == 0
        assert data["coverage"]["sast"]["state"] == "completed"
        assert data["coverage"]["iac"]["state"] == "incomplete"
        assert data["compliance"]["state"] == "not_assessed"
        assert data["ai_fix"]["state"] == "disabled"
        second = client.get(url, params={"limit": 1, "offset": 1}).json()
        assert data["findings"][0]["id"] != second["findings"][0]["id"]
        assert client.get(url, params={"offset": 2}).json()["findings"] == []
        assert client.get(url, params={"limit": 501}).status_code == 422
        assert client.get(url, params={"offset": -1}).status_code == 422
        assert client.post(url, json={}).status_code == 405


@pytest.mark.parametrize("username", ["outsider", "admin"])
def test_foreign_users_cannot_read(world: dict[str, Any], username: str) -> None:
    srv = world["srv"]
    caller = srv.login(username, PASSWORD)
    assert caller is not None
    with pytest.raises(NotFoundError):
        srv.security_center(caller, world["project"], world["run"])


def test_project_scope_and_auditor(world: dict[str, Any]) -> None:
    srv = world["srv"]
    with pytest.raises(NotFoundError):
        srv.security_center(world["owner"], world["other"], world["run"])
    scoped = replace(world["owner"], token_project_id=world["other"])
    with pytest.raises(NotFoundError):
        srv.security_center(scoped, world["project"], world["run"])
    auditor = srv.login("auditor", PASSWORD)
    assert auditor is not None
    assert srv.security_center(auditor, world["project"], world["run"])["summary"]["total"] == 2
