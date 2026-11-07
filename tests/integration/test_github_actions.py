"""GitHub Actions through the server (ADR-0043): the repository and its token per project,
workflows, runs and jobs, a dispatch — with errors people can act on and a token that
never leaves the server. GitHub is an in-memory double (``httpx.MockTransport``)."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import AuditEvent

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
KEY = base64.b64encode(os.urandom(32)).decode()
TOKEN = "ghp_TopSecretToken_abcdef1234567890"
RUN = {
    "id": 501,
    "name": "CI",
    "display_title": "Fix the cart",
    "workflow_id": 9,
    "path": ".github/workflows/ci.yml",
    "status": "completed",
    "conclusion": "success",
    "head_branch": "main",
    "head_sha": "a" * 40,
    "event": "push",
    "actor": {"login": "octo"},
    "run_number": 12,
    "run_started_at": "2026-11-06T10:00:00Z",
    "updated_at": "2026-11-06T10:03:20Z",
    "html_url": "https://github.com/acme/shop/actions/runs/501",
}


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.mode = "ok"

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - a router
        self.calls.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("no route to host", request=request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}" or self.mode == "bad_token":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if self.mode == "forbidden":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if self.mode == "rate":
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "retry-after": "3600"},
            )
        path = request.url.path
        base = "/repos/acme/shop"
        if path == base:
            return httpx.Response(
                200,
                json={
                    "full_name": "acme/shop",
                    "private": True,
                    "default_branch": "main",
                    "permissions": {"push": True},
                    "html_url": "https://github.com/acme/shop",
                },
            )
        if path == f"{base}/actions/workflows":
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "workflows": [
                        {
                            "id": 9,
                            "name": "CI",
                            "path": ".github/workflows/ci.yml",
                            "state": "active",
                            "html_url": "https://github.com/acme/shop/actions/workflows/ci.yml",
                        },
                        {
                            "id": 10,
                            "name": "Deploy",
                            "path": ".github/workflows/deploy.yml",
                            "state": "active",
                            "html_url": "https://github.com/acme/shop/blob/main/x",
                        },
                    ],
                },
            )
        if path in (f"{base}/actions/runs", f"{base}/actions/workflows/ci.yml/runs"):
            return httpx.Response(200, json={"total_count": 1, "workflow_runs": [RUN]})
        if path == f"{base}/actions/runs/501/jobs":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 1,
                            "name": "test",
                            "status": "completed",
                            "conclusion": "failure",
                            "started_at": "2026-11-06T10:00:05Z",
                            "completed_at": "2026-11-06T10:03:00Z",
                            "html_url": "https://github.com/acme/shop/actions/runs/501/job/1",
                            "steps": [
                                {
                                    "number": 1,
                                    "name": "pytest",
                                    "status": "completed",
                                    "conclusion": "failure",
                                }
                            ],
                        },
                    ]
                },
            )
        if path == f"{base}/actions/workflows/deploy.yml/dispatches":
            return httpx.Response(204)
        if path == f"{base}/actions/workflows/missing.yml/dispatches":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    roots = tmp_path / "repos"
    (roots / "alpha").mkdir(parents=True)
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 's.db'}",
            allowed_roots=(roots.resolve(),),
            secret_key=KEY,
        )
    )
    srv.migrate()
    fake = FakeGitHub()
    srv.github_transport = httpx.MockTransport(fake)
    for name in ("owner", "maint", "dev", "auditor", "outsider"):
        srv.create_user(name, PASSWORD)
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    pid = srv.create_project(owner, "alpha", path=str(roots.resolve() / "alpha"))
    srv.set_member(owner, pid, "maint", Role.MAINTAINER)
    srv.set_member(owner, pid, "dev", Role.DEVELOPER)
    srv.set_member(owner, pid, "auditor", Role.AUDITOR)
    with TestClient(create_app(srv, broker=None)) as client:
        heads = {}
        for name in ("owner", "maint", "dev", "auditor", "outsider"):
            token = client.post(
                "/api/v1/auth/login", json={"username": name, "password": PASSWORD}
            ).json()["token"]
            heads[name] = {"Authorization": f"Bearer {token}"}
        yield {"c": client, "srv": srv, "p": pid, "h": heads, "fake": fake}
    srv.close()


def connect(w: dict[str, Any], *, enabled: bool = True) -> None:
    c, p, h = w["c"], w["p"], w["h"]
    body = {"repository": "https://github.com/acme/shop.git", "default_ref": "main"}
    r = c.put(f"/api/v1/projects/{p}/github", json=body | {"enabled": enabled}, headers=h["maint"])
    assert r.status_code == 200, r.text
    r = c.put(
        f"/api/v1/projects/{p}/secrets/github.token", json={"value": TOKEN}, headers=h["owner"]
    )
    assert r.status_code == 200, r.text


def test_settings_are_validated_and_the_token_is_never_returned(world: dict[str, Any]) -> None:
    c, p, h = world["c"], world["p"], world["h"]
    url = f"/api/v1/projects/{p}/github"
    empty = c.get(url, headers=h["dev"]).json()
    assert empty["data"] == {} and empty["token_set"] is False and empty["can_manage"] is False
    for body in (
        {"repository": "not a repo"},
        {"repository": "acme/shop", "api_url": "http://gh.example.com"},
        {"repository": "acme/shop", "api_url": "https://10.0.0.7/api/v3"},
        {"repository": "acme/shop", "default_ref": "a..b"},
        {"repository": "acme/shop", "x": 1},
    ):
        r = c.put(url, json=body, headers=h["owner"])
        assert r.status_code == 422, (body, r.text)
    assert c.put(url, json={"repository": "acme/shop"}, headers=h["dev"]).status_code == 403
    assert c.put(url, json={"repository": "bad"}, headers=h["outsider"]).status_code == 404
    assert c.get(url, headers=h["outsider"]).status_code == 404

    connect(world)
    mine = c.get(url, headers=h["owner"]).json()
    assert mine["data"] == {
        "enabled": True,
        "repository": "acme/shop",
        "api_url": "https://api.github.com",
        "default_ref": "main",
    }
    assert mine["enabled"] is True
    assert mine["token_set"] is True and mine["can_manage"] is True
    assert mine["token_hint"] and TOKEN not in json.dumps(mine)
    theirs = c.get(url, headers=h["dev"]).json()
    assert theirs["token_set"] is True and theirs["token_hint"] is None
    stale = c.put(
        url, json={"repository": "acme/other", "version": mine["version"] - 1}, headers=h["owner"]
    )
    assert stale.status_code == 409  # changed meanwhile


def test_workflows_runs_jobs_and_dispatch(world: dict[str, Any]) -> None:
    c, p, h, fake = world["c"], world["p"], world["h"], world["fake"]
    base = f"/api/v1/projects/{p}/github"
    r = c.get(f"{base}/runs", headers=h["dev"])
    assert r.status_code == 409 and "repository" in r.json()["error"]["message"]
    connect(world)

    check = c.get(f"{base}/check", headers=h["auditor"]).json()
    assert check["ok"] and check["repository"] == "acme/shop" and check["default_branch"] == "main"
    flows = c.get(f"{base}/workflows", headers=h["auditor"]).json()
    assert [f["file"] for f in flows] == ["ci.yml", "deploy.yml"]
    [run] = c.get(f"{base}/runs?workflow=ci.yml&branch=main&limit=5", headers=h["dev"]).json()
    assert run["id"] == 501 and run["title"] == "Fix the cart" and run["conclusion"] == "success"
    assert run["branch"] == "main" and run["actor"] == "octo" and run["duration_seconds"] == 200
    last = fake.calls[-1]
    assert last.url.params["branch"] == "main" and last.url.params["per_page"] == "100"
    assert c.get(f"{base}/runs?workflow=../x", headers=h["dev"]).status_code == 422
    [job] = c.get(f"{base}/runs/501/jobs", headers=h["dev"]).json()
    assert job["conclusion"] == "failure" and job["steps"][0]["name"] == "pytest"
    assert c.get(f"{base}/runs/abc/jobs", headers=h["dev"]).status_code == 404

    url = f"{base}/workflows/deploy.yml/dispatch"
    assert c.post(url, json={"ref": "main"}, headers=h["auditor"]).status_code == 403
    r = c.post(url, json={"ref": "main", "inputs": {"environment": "staging"}}, headers=h["dev"])
    assert r.status_code == 200 and r.json() == {
        "ok": True,
        "workflow": "deploy.yml",
        "ref": "main",
    }
    sent = json.loads(fake.calls[-1].content)
    assert sent == {"ref": "main", "inputs": {"environment": "staging"}}
    bad = c.post(url, json={"ref": "main", "inputs": {"bad name": "x"}}, headers=h["dev"])
    assert bad.status_code == 422
    missing = c.post(
        f"{base}/workflows/missing.yml/dispatch", json={"ref": "main"}, headers=h["dev"]
    )
    assert missing.status_code == 502 and "404" in missing.json()["error"]["message"]
    with world["srv"].session() as s:
        events = s.scalars(select(AuditEvent).where(AuditEvent.action == "github.dispatch")).all()
    assert [e.target for e in events] == ["deploy.yml"]
    assert events[0].details == {"ref": "main", "inputs": ["environment"]}


@pytest.mark.parametrize(
    ("mode", "status", "words"),
    [
        ("bad_token", 502, "rejected the token"),
        ("forbidden", 502, "may not do this"),
        ("rate", 502, "rate limit"),
        ("down", 502, "unreachable"),
    ],
)
def test_failures_are_explained_without_the_token(
    world: dict[str, Any], mode: str, status: int, words: str
) -> None:
    connect(world)
    world["fake"].mode = mode
    r = world["c"].get(f"/api/v1/projects/{world['p']}/github/runs", headers=world["h"]["dev"])
    assert r.status_code == status and words in r.json()["error"]["message"], r.text
    assert TOKEN not in r.text


def test_turned_off_the_server_leaves_github_alone_but_a_check_still_works(
    world: dict[str, Any],
) -> None:
    c, p, h, fake = world["c"], world["p"], world["h"], world["fake"]
    base = f"/api/v1/projects/{p}/github"
    # Nothing configured: the check explains what is missing.
    empty = c.get(f"{base}/check", headers=h["dev"]).json()
    assert empty["ok"] is False and "repository" in empty["error"]
    assert empty["reason"] == "no_repository"
    connect(world, enabled=False)
    assert c.get(base, headers=h["dev"]).json()["enabled"] is False
    for path in ("runs", "workflows", "runs/501/jobs"):
        r = c.get(f"{base}/{path}", headers=h["dev"])
        assert r.status_code == 409 and "turned off" in r.json()["error"]["message"], path
    r = c.post(f"{base}/workflows/deploy.yml/dispatch", json={"ref": "main"}, headers=h["dev"])
    assert r.status_code == 409
    assert fake.calls == []  # not a single call to GitHub

    ok = c.get(f"{base}/check", headers=h["dev"]).json()  # an explicit check is allowed
    assert ok["ok"] is True and ok["permissions"] == {"push": True} and ok["checked_at"]
    fake.mode = "bad_token"
    bad = c.get(f"{base}/check", headers=h["dev"])
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert "rejected the token" in bad.json()["error"] and TOKEN not in bad.text
    assert bad.json()["reason"] == "bad_token"
    assert c.get(base, headers=h["dev"]).json()["last"]["ok"] is False


def test_the_overview_shows_every_visible_project(world: dict[str, Any]) -> None:
    c, h, srv, fake = world["c"], world["h"], world["srv"], world["fake"]
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    zeta = srv.settings.allowed_roots[0] / "zeta"
    zeta.mkdir()
    hidden = srv.create_project(owner, "zeta", path=str(zeta))
    status = c.get("/api/v1/github/status", headers=h["dev"]).json()
    assert [r["name"] for r in status["projects"]] == ["alpha"]  # zeta: dev is not a member
    assert status["connected"] == 0 and status["projects"][0]["last"] is None
    connect(world)
    c.get(f"/api/v1/projects/{world['p']}/github/runs", headers=h["dev"])
    status = c.get("/api/v1/github/status", headers=h["dev"]).json()
    [row] = status["projects"]
    assert row["connected"] and row["enabled"] and row["repository"] == "acme/shop"
    assert row["last"]["ok"] is True and row["last"]["kind"] == "poll"
    assert (status["connected"], status["enabled"], status["failing"]) == (1, 1, 0)
    fake.mode = "forbidden"
    c.get(f"/api/v1/projects/{world['p']}/github/runs", headers=h["dev"])
    status = c.get("/api/v1/github/status", headers=h["dev"]).json()
    assert status["failing"] == 1 and "may not do this" in status["projects"][0]["last"]["error"]
    assert status["projects"][0]["last"]["reason"] == "forbidden"
    assert TOKEN not in json.dumps(status) and status["projects"][0]["can_manage"] is False
    mine = c.get("/api/v1/github/status", headers=h["owner"]).json()
    assert {r["name"] for r in mine["projects"]} == {"alpha", "zeta"}
    assert str(hidden) in {r["project_id"] for r in mine["projects"]}
    assert c.get("/api/v1/github/status", headers=h["outsider"]).json()["projects"] == []


def test_outsiders_learn_nothing(world: dict[str, Any]) -> None:
    connect(world)
    c, p, h = world["c"], world["p"], world["h"]
    calls = len(world["fake"].calls)
    for method, path, body in [
        ("GET", "github", None),
        ("GET", "github/check", None),
        ("GET", "github/workflows", None),
        ("GET", "github/runs", None),
        ("GET", "github/runs/501/jobs", None),
        ("POST", "github/workflows/deploy.yml/dispatch", {"ref": "main"}),
    ]:
        r = c.request(method, f"/api/v1/projects/{p}/{path}", json=body, headers=h["outsider"])
        assert r.status_code == 404, (path, r.text)
    assert len(world["fake"].calls) == calls  # GitHub was never asked
