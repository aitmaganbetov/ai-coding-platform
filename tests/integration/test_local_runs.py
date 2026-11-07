"""Local (CLI) runs reported to the server (ADR-0047): the API and the CLI end to end."""

from __future__ import annotations

import stat
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from ai_coding.api.app import create_app
from ai_coding.api.services import Principal, Server
from ai_coding.api.settings import ServerSettings
from ai_coding.cli import remote
from ai_coding.cli.app import app as cli
from ai_coding.domain.enums import Role
from ai_coding.git import LocalGitRepository
from ai_coding.persistence.models import AuditEvent

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
runner = CliRunner()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    for name in (remote.URL_ENV, remote.TOKEN_ENV, remote.PROJECT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    roots = tmp_path / "roots"
    (roots / "alpha").mkdir(parents=True)
    srv = Server(ServerSettings(data_dir=tmp_path / "data",
                                database_url=f"sqlite:///{tmp_path / 'server.db'}",
                                allowed_roots=(roots.resolve(),)))  # fmt: skip
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    for name in ("dev", "aud", "outsider"):
        srv.create_user(name, PASSWORD)
    admin = srv.login("admin", PASSWORD)
    assert admin is not None
    alpha = srv.create_project(admin, "alpha", path=str(roots.resolve() / "alpha"))
    srv.set_member(admin, alpha, "dev", Role.DEVELOPER)
    srv.set_member(admin, alpha, "aud", Role.AUDITOR)
    users: dict[str, Principal] = {"admin": admin}
    for name in ("dev", "aud", "outsider"):
        principal = srv.login(name, PASSWORD)
        assert principal is not None
        users[name] = principal
    headers = {n: {"Authorization": f"Bearer {srv.issue_token(p, 'laptop')[0]}"}
               for n, p in users.items()}  # fmt: skip
    with TestClient(create_app(srv, broker=None)) as client:
        yield {"server": srv, "client": client, "alpha": alpha, "headers": headers, "users": users}
    srv.close()


def _report(**extra: Any) -> dict[str, Any]:
    return {"run_id": str(uuid.uuid4()), "machine": "laptop-7", "cli_version": "2.1.0",
            "mode": "audit", "goal": "audit", "state": "COMPLETED", "verdict": "FAIL",
            "branch": "main", "commit": "a" * 40,
            "gates": [{"gate": "security", "result": "FAIL", "blocking": True,
                       "reason": "1 new high finding"}],
            "findings": {"high": 1, "low": 2}, "tasks": {"SUCCEEDED": 5},
            "started_at": "2026-11-06T10:00:00+00:00",
            "finished_at": "2026-11-06T10:02:00+00:00", **extra}  # fmt: skip


def test_reports_are_recorded_idempotently_and_only_by_who_may_run(world: dict[str, Any]) -> None:
    client, h, alpha = world["client"], world["headers"], world["alpha"]
    url = f"/api/v1/projects/{alpha}/local-runs"
    body = _report()
    first = client.post(url, json=body, headers=h["dev"])
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True and first.json()["reported_by"] == "dev"
    again = client.post(url, json={**body, "state": "FAILED", "error": "boom"}, headers=h["dev"])
    assert again.json()["created"] is False and again.json()["state"] == "FAILED"
    # Another member cannot overwrite it; an auditor may read but not report; a stranger
    # learns nothing.
    assert client.post(url, json=body, headers=h["admin"]).status_code == 409
    assert client.post(url, json=_report(), headers=h["aud"]).status_code == 403
    assert client.post(url, json=_report(), headers=h["outsider"]).status_code == 404
    assert client.get(url, headers=h["outsider"]).status_code == 404
    # Validation comes after the access check, and refuses anything but a summary.
    for bad in ({"state": "done"}, {"commit": "not-a-sha"}, {"code": "print(1)"},
                {"findings": {"high": -1}}, {"started_at": "2026-11-06T10:00:00"}):  # fmt: skip
        r = client.post(url, json=_report(**bad), headers=h["dev"])
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid", bad
    rows = client.get(url, headers=h["aud"]).json()
    assert [r["run_id"] for r in rows] == [body["run_id"]]
    assert rows[0]["gates"][0]["gate"] == "security" and rows[0]["findings"] == {
        "high": 1,
        "low": 2,
    }
    overview = client.get("/api/v1/local-runs/overview", headers=h["dev"]).json()
    assert overview["reports_24h"] == 1 and overview["projects"] == 1
    (machine,) = overview["machines"]
    assert machine["machine"] == "laptop-7" and machine["user"] == "dev"
    assert machine["project"] == "alpha" and machine["state"] == "FAILED"
    empty = client.get("/api/v1/local-runs/overview", headers=h["outsider"]).json()
    assert empty == {"machines": [], "reports_24h": 0, "projects": 0, "last_report": None}
    with world["server"].session() as s:
        actions = s.scalars(select(AuditEvent.action).where(AuditEvent.project_id == alpha)).all()
    assert actions.count("local_run.report") == 2


@pytest.fixture
def linked(world: dict[str, Any], git_repo: LocalGitRepository,
           monkeypatch: pytest.MonkeyPatch) -> Path:  # fmt: skip
    """A CLI project signed in to the test server: the CLI's requests go to the app
    in-process (Starlette's TestClient: same requests, an ASGI transport)."""
    api = world["client"].app

    def init(self: remote.Client, creds: remote.Credentials, **_: Any) -> None:
        self.url = creds.url
        self._http = TestClient(api, base_url=f"{creds.url}/api/v1",  # type: ignore[assignment]
                                headers={"Authorization": f"Bearer {creds.token}"})  # fmt: skip

    monkeypatch.setattr(remote.Client, "__init__", init)
    root = git_repo.root
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    assert runner.invoke(cli, ["init", str(root), "--mode", "develop"]).exit_code == 0
    token = world["headers"]["dev"]["Authorization"].removeprefix("Bearer ")
    login = runner.invoke(cli, ["server", "login", "--url", "http://testserver/", "--token-stdin"],
                          input=token + "\n")  # fmt: skip
    assert login.exit_code == 0, login.output
    assert "signed in to http://testserver as dev" in login.output
    assert "plain HTTP" in login.stderr  # the token would travel unencrypted
    saved = remote.credentials_path()
    if sys.platform != "win32":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert token in saved.read_text(encoding="utf-8")
    return root


def test_the_cli_reports_its_runs_to_the_linked_project(
    world: dict[str, Any], linked: Path
) -> None:
    client, h, alpha = world["client"], world["headers"], world["alpha"]
    unlinked = runner.invoke(cli, ["run", "discover", "-C", str(linked)])
    assert unlinked.exit_code == 0 and "reported" not in unlinked.stderr  # not linked: silent
    nope = runner.invoke(cli, ["server", "link", "beta", "-C", str(linked)])
    assert nope.exit_code != 0 and "alpha" in nope.stderr  # names the user's projects
    link = runner.invoke(cli, ["server", "link", "alpha", "-C", str(linked)])
    assert link.exit_code == 0, link.output
    assert (linked / ".ai-coding" / "server.json").is_file()  # local state: never committed
    run = runner.invoke(cli, ["run", "discover", "-C", str(linked)])
    assert run.exit_code == 0, run.output
    assert "reported to http://testserver (project alpha)" in run.stderr
    (row,) = client.get(f"/api/v1/projects/{alpha}/local-runs", headers=h["dev"]).json()
    assert row["state"] == "COMPLETED" and row["reported_by"] == "dev"
    assert row["branch"] == "main" and len(row["commit"]) == 40
    assert row["machine"] and row["tasks"] and row["verdict"] is None  # not an audit
    resent = runner.invoke(cli, ["server", "report", "-C", str(linked)])
    assert resent.exit_code == 0, resent.output
    assert f"run {row['run_id'][:8]} updated" in resent.output
    who = runner.invoke(cli, ["server", "whoami", "-C", str(linked)])
    assert "as dev" in who.output and "reports to project alpha" in who.output
    assert runner.invoke(cli, ["server", "unlink", "-C", str(linked)]).exit_code == 0
    assert runner.invoke(cli, ["server", "logout"]).exit_code == 0
    assert remote.load_credentials() is None


def test_a_failed_report_never_fails_the_command(world: dict[str, Any], linked: Path) -> None:
    remote.save_link(linked, remote.Link(str(uuid.uuid4()), "gone", "http://testserver"))
    run = runner.invoke(cli, ["run", "discover", "-C", str(linked)])
    assert run.exit_code == 0, run.output
    assert "not reported" in run.stderr and "HTTP 404" in run.stderr
    assert "ai-coding server report" in run.stderr  # how to send it later
    # Linked to another server than the one signed in to: nothing is sent there.
    remote.save_link(linked, remote.Link(str(world["alpha"]), "alpha", "https://other.example"))
    other = runner.invoke(cli, ["run", "discover", "-C", str(linked)])
    assert other.exit_code == 0 and "linked to https://other.example" in other.stderr
    assert world["client"].get(f"/api/v1/projects/{world['alpha']}/local-runs",
                               headers=world["headers"]["dev"]).json() == []  # fmt: skip
