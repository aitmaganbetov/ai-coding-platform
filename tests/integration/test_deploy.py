"""Deployments (ADR-0048): environments, a deploy run with gates, approval, a signed
webhook or a GitHub Actions workflow, the health check and the history.

GitHub and the webhook/health endpoints are in-memory doubles (``httpx.MockTransport``)
plugged into the server's test-only transports; the public-address rule (SSRF) is
tested on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_coding.api.app import create_app
from ai_coding.api.deploys import secret_name
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import AuditEvent, Run, Task
from ai_coding.workflows import deploy as flow

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
KEY = base64.b64encode(os.urandom(32)).decode()
HOOK = "https://deploy.example.com/hooks/ai"
HEALTH = "https://staging.example.com/healthz"
SHA = "c0ffee" + "0" * 34


class Remote:
    """The webhook receiver, the health endpoint and a fake GitHub API."""

    def __init__(self) -> None:
        self.hooks: list[httpx.Request] = []
        self.health: list[int] = []
        self.hook_status = 200
        self.health_codes = [200]
        self.github: list[tuple[str, str, Any]] = []
        self.conclusion = "success"
        self.polls = 0
        self.dispatched_at: str | None = None

    # webhook and health (async client of the deploy steps)
    def web(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/hooks/ai":
            self.hooks.append(request)
            return httpx.Response(self.hook_status, json={"url": "https://cd.example.com/d/42"})
        if request.url.path == "/healthz":
            code = self.health_codes.pop(0) if len(self.health_codes) > 1 else self.health_codes[0]
            self.health.append(code)
            return httpx.Response(code)
        return httpx.Response(404)

    # GitHub (sync client of the server)
    def api(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - a router
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.github.append((method, path, body))
        assert request.headers["Authorization"] == "Bearer ghp_test_token_123456"
        base = "/repos/acme/shop"
        if path == f"{base}/commits/main":
            return httpx.Response(200, json={"sha": SHA})
        if path.startswith(f"{base}/commits/"):
            return httpx.Response(404, json={"message": "No commit found"})
        if path == f"{base}/actions/workflows/deploy.yml/dispatches" and method == "POST":
            self.dispatched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            return httpx.Response(204)
        run = {
            "id": 777,
            "name": "Deploy",
            "status": "in_progress",
            "conclusion": None,
            "head_branch": "main",
            "head_sha": SHA,
            "event": "workflow_dispatch",
            "created_at": self.dispatched_at,
            "html_url": "https://github.com/acme/shop/actions/runs/777",
        }
        if path == f"{base}/actions/workflows/deploy.yml/runs":
            return httpx.Response(200, json={"workflow_runs": [run] if self.dispatched_at else []})
        if path == f"{base}/actions/runs/777":
            self.polls += 1
            if self.polls >= 2:
                run |= {"status": "completed", "conclusion": self.conclusion}
            return httpx.Response(200, json=run)
        if path == f"{base}/actions/runs/777/cancel":
            return httpx.Response(202)
        return httpx.Response(404, json={"message": "Not Found"})


def git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setattr(flow, "POLL_SECONDS", 0.01)
    roots = tmp_path / "repos"
    alpha = roots / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "app.py").write_text("print('v1')\n", encoding="utf-8")
    git(alpha, "init", "-q", "-b", "main")
    git(alpha, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".")
    git(alpha, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "v1")
    (roots / "beta").mkdir()
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 's.db'}",
            allowed_roots=(roots.resolve(),),
            secret_key=KEY,
        )
    )
    srv.migrate()
    remote = Remote()
    srv.github_transport = httpx.MockTransport(remote.api)
    srv.deploy_test_transport = httpx.MockTransport(remote.web)
    for name in ("owner", "maint", "dev", "outsider"):
        srv.create_user(name, PASSWORD)
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    pid = srv.create_project(owner, "alpha", path=str(alpha.resolve()))
    beta = srv.create_project(owner, "beta", path=str((roots / "beta").resolve()))
    srv.set_member(owner, pid, "maint", Role.MAINTAINER)
    srv.set_member(owner, pid, "dev", Role.DEVELOPER)
    with TestClient(create_app(srv, broker=None)) as client:
        heads = {}
        for name in ("owner", "maint", "dev", "outsider"):
            token = client.post(
                "/api/v1/auth/login", json={"username": name, "password": PASSWORD}
            ).json()["token"]
            heads[name] = {"Authorization": f"Bearer {token}"}
        yield {
            "c": client,
            "srv": srv,
            "p": pid,
            "beta": beta,
            "h": heads,
            "remote": remote,
            "head": git(alpha, "rev-parse", "HEAD"),
        }
    srv.close()


def env_body(**over: Any) -> dict[str, Any]:
    return {"name": "staging", "kind": "webhook", "webhook_url": HOOK, "health_url": HEALTH} | over


def create_env(w: dict[str, Any], **over: Any) -> dict[str, Any]:
    r = w["c"].post(
        f"/api/v1/projects/{w['p']}/environments", json=env_body(**over), headers=w["h"]["owner"]
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def set_secret(w: dict[str, Any], name: str, value: str) -> None:
    r = w["c"].put(
        f"/api/v1/projects/{w['p']}/secrets/{name}", json={"value": value}, headers=w["h"]["owner"]
    )
    assert r.status_code == 200, r.text


def deploy(w: dict[str, Any], env: dict[str, Any], ref: str = "main", who: str = "maint") -> Any:
    return w["c"].post(
        f"/api/v1/projects/{w['p']}/deployments",
        json={"environment_id": env["id"], "ref": ref},
        headers=w["h"][who],
    )


async def drain(w: dict[str, Any]) -> None:
    await Worker(w["srv"], poll_seconds=0.02).run_until_idle(timeout=60)


def history(w: dict[str, Any], who: str = "dev") -> list[dict[str, Any]]:
    r = w["c"].get(f"/api/v1/projects/{w['p']}/deployments", headers=w["h"][who])
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# ------------------------------------------------------------ environments --
def test_environments_are_validated_and_managed_by_owners_and_maintainers(
    world: dict[str, Any],
) -> None:
    c, p, h = world["c"], world["p"], world["h"]
    url = f"/api/v1/projects/{p}/environments"
    bad = [
        env_body(webhook_url="http://deploy.example.com/hook"),  # not https
        env_body(webhook_url="https://127.0.0.1/hook"),  # loopback
        env_body(webhook_url="https://10.1.2.3/hook"),  # private
        env_body(webhook_url="https://169.254.169.254/latest"),  # link-local (metadata)
        env_body(webhook_url="https://localhost/hook"),
        env_body(webhook_url="https://deployer.internal/hook"),
        env_body(health_url="https://[::1]/healthz"),
        env_body(kind="github_actions", webhook_url=None),  # no workflow
        env_body(kind="github_actions", workflow="../evil.yml"),
        env_body(name="no/slashes"),
        env_body(kind="ftp"),
        env_body(webhook_url=None),
    ]
    for body in bad:
        r = c.post(url, json=body, headers=h["owner"])
        assert r.status_code == 422, (body, r.text)
    # Developers and outsiders: no changes; outsiders don't even see the project.
    assert c.post(url, json=env_body(), headers=h["dev"]).status_code == 403
    assert c.post(url, json=env_body(), headers=h["outsider"]).status_code == 404
    assert c.post(url, json={}, headers=h["outsider"]).status_code == 404  # access first

    staging = create_env(world)
    assert staging["secret_name"] == "deploy.staging.webhook" == secret_name("staging")
    assert staging["requires_approval"] is False and staging["last_deployment"] is None
    r = c.post(url, json=env_body(name="Staging"), headers=h["maint"])
    assert r.status_code == 409  # same secret name: refused
    prod = c.post(
        url, json=env_body(name="production", production=True, position=1), headers=h["maint"]
    ).json()
    assert prod["requires_approval"] is True  # production always waits for a person
    gha = c.post(
        url,
        json={
            "name": "preview",
            "kind": "github_actions",
            "workflow": "deploy.yml",
            "ref_input": "ref",
        },
        headers=h["owner"],
    ).json()
    assert gha["workflow"] == "deploy.yml" and gha["webhook_url"] is None

    listing = c.get(url, headers=h["dev"]).json()
    assert [e["name"] for e in listing["environments"]] == ["preview", "staging", "production"]
    assert listing["can_manage"] is False and listing["can_deploy"] is False
    mine = c.get(url, headers=h["maint"]).json()
    assert mine["can_manage"] is True and mine["can_deploy"] is True
    assert c.get(url, headers=h["outsider"]).status_code == 404

    # A rename carries the signing secret along.
    set_secret(world, "deploy.staging.webhook", "s3cr3t-signing-key")
    r = c.put(f"{url}/{staging['id']}", json=env_body(name="stage"), headers=h["owner"])
    assert r.status_code == 200 and r.json()["secret_name"] == "deploy.stage.webhook"
    names = {
        s["name"]
        for s in c.get(f"/api/v1/projects/{p}/secrets", headers=h["owner"]).json()["secrets"]
    }
    assert "deploy.stage.webhook" in names and "deploy.staging.webhook" not in names
    assert world["srv"].secret_value(p, "deploy.stage.webhook") == "s3cr3t-signing-key"
    assert c.put(f"{url}/{staging['id']}", json=env_body(), headers=h["dev"]).status_code == 403
    assert c.put(f"{url}/not-an-id", json=env_body(), headers=h["owner"]).status_code == 404
    other = c.put(
        f"/api/v1/projects/{world['beta']}/environments/{staging['id']}",
        json=env_body(),
        headers=h["owner"],
    )
    assert other.status_code == 404  # an environment of another project

    assert c.delete(f"{url}/{staging['id']}", headers=h["dev"]).status_code == 403
    assert c.delete(f"{url}/{staging['id']}", headers=h["owner"]).status_code == 204
    names = {
        s["name"]
        for s in c.get(f"/api/v1/projects/{p}/secrets", headers=h["owner"]).json()["secrets"]
    }
    assert "deploy.stage.webhook" not in names  # its secret went with it


# ---------------------------------------------------------------- webhook --
async def test_a_webhook_deployment_is_signed_checked_and_recorded(world: dict[str, Any]) -> None:
    remote: Remote = world["remote"]
    env = create_env(world)
    set_secret(world, "deploy.staging.webhook", "s3cr3t-signing-key")
    remote.health_codes = [503, 200]  # the service needs a moment
    assert deploy(world, env, who="dev").status_code == 403  # developers do not deploy
    r = deploy(world, env)
    assert r.status_code == 201, r.text
    started = r.json()
    assert started["state"] == "queued" and started["run_id"]
    assert deploy(world, env).status_code == 409  # one deployment at a time
    await drain(world)

    [request] = remote.hooks
    body = request.content
    expected = "sha256=" + hmac.new(b"s3cr3t-signing-key", body, hashlib.sha256).hexdigest()
    assert request.headers[flow.SIGNATURE_HEADER] == expected
    payload = json.loads(body)
    assert payload["environment"] == "staging" and payload["ref"] == "main"
    assert payload["commit"] == world["head"] and payload["requested_by"] == "maint"
    assert payload["deployment_id"] == started["id"] and payload["production"] is False
    assert "s3cr3t" not in body.decode()
    assert remote.health == [503, 200]

    [done] = history(world)
    assert done["state"] == "succeeded", done
    assert done["commit"] == world["head"] and done["external_url"] == "https://cd.example.com/d/42"
    assert done["environment"] == "staging" and done["finished_at"]
    status = world["srv"].run_status(
        world["srv"].login("maint", PASSWORD), uuid.UUID(done["run_id"])
    )
    assert status["state"] == "COMPLETED"
    envs = world["c"].get(f"/api/v1/projects/{world['p']}/environments", headers=world["h"]["dev"])
    assert envs.json()["environments"][0]["last_deployment"]["id"] == done["id"]
    with world["srv"].session() as s:
        actions = set(s.scalars(select(AuditEvent.action)))
    assert {"deploy.start", "deploy.environment.create"} <= actions


async def test_a_failing_webhook_or_health_check_fails_the_deployment(
    world: dict[str, Any],
) -> None:
    remote: Remote = world["remote"]
    env = create_env(world)
    r = deploy(world, env)
    await drain(world)
    first = history(world)[0]
    assert first["id"] == r.json()["id"] and first["state"] == "failed"
    assert "signing secret" in first["detail"]  # no secret: nothing is sent unsigned
    assert remote.hooks == []

    set_secret(world, "deploy.staging.webhook", "k" * 32)
    remote.hook_status = 500
    deploy(world, env)
    await drain(world)
    assert history(world)[0]["state"] == "failed" and "500" in history(world)[0]["detail"]

    remote.hook_status, remote.health_codes = 200, [502]
    deploy(world, env)
    await drain(world)
    newest = history(world)[0]
    assert newest["state"] == "failed" and "health check" in newest["detail"]
    assert len(remote.health) == flow.HEALTH_ATTEMPTS
    assert [d["state"] for d in history(world)] == ["failed", "failed", "failed"]  # newest first

    unknown = deploy(world, env, ref="no-such-branch")
    assert unknown.status_code == 201
    await drain(world)
    assert "not found" in history(world)[0]["detail"]
    assert deploy(world, env, ref="a..b").status_code == 422
    bad_env = world["c"].post(
        f"/api/v1/projects/{world['p']}/deployments",
        json={"environment_id": str(uuid.uuid4()), "ref": "main"},
        headers=world["h"]["maint"],
    )
    assert bad_env.status_code == 404


# --------------------------------------------------------------- approval --
async def test_production_waits_for_a_person_and_a_rejection_stops_it(
    world: dict[str, Any],
) -> None:
    remote: Remote = world["remote"]
    srv: Server = world["srv"]
    prod = create_env(world, name="production", production=True)
    set_secret(world, "deploy.production.webhook", "p" * 32)
    started = deploy(world, prod).json()
    await drain(world)
    waiting = history(world)[0]
    assert waiting["state"] == "awaiting_approval" and remote.hooks == []
    run_id = uuid.UUID(started["run_id"])
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    assert srv.run_status(owner, run_id)["state"] == "AWAITING_APPROVAL"
    approve = world["c"].post(
        f"/api/v1/runs/{run_id}/approve", json={"note": "go"}, headers=world["h"]["owner"]
    )
    assert approve.status_code == 200, approve.text
    await drain(world)
    assert history(world)[0]["state"] == "succeeded"
    assert len(remote.hooks) == 1 and json.loads(remote.hooks[0].content)["production"] is True

    second = deploy(world, prod).json()
    await drain(world)
    assert srv.decide(owner, uuid.UUID(second["run_id"]), approve=False, note="not today")
    await drain(world)
    rejected = history(world)[0]
    assert rejected["id"] == second["id"]
    assert rejected["state"] in {"failed", "cancelled"} and rejected["finished_at"]
    assert len(remote.hooks) == 1  # nothing was sent


# ----------------------------------------------------------------- gates --
async def test_required_gates_refuse_a_deployment_without_a_passing_audit(
    world: dict[str, Any],
) -> None:
    srv: Server = world["srv"]
    env = create_env(world, require_gates=True)
    set_secret(world, "deploy.staging.webhook", "g" * 32)
    r = deploy(world, env)
    assert r.status_code == 409 and "audit" in r.json()["error"]["message"]

    # A completed audit with a failed blocking gate still refuses; a passing one allows.
    with srv.session() as s:
        run = Run(
            project_id=world["p"],
            mode="audit",
            goal="audit",
            state="COMPLETED",
            requested_by="owner",
            options={"kind": "audit"},
        )
        s.add(run)
        s.flush()
        task = Task(
            run_id=run.id,
            key="quality.gates",
            name="gates",
            phase="VERIFICATION",
            capability="quality.gates",
            status="COMPLETED",
            output={"gates": [{"gate": "security", "result": "FAIL", "blocking": True}]},
        )
        s.add(task)
        s.flush()
        task_id = task.id
    r = deploy(world, env)
    assert r.status_code == 409 and "security" in r.json()["error"]["message"]
    with srv.session() as s:
        row = s.get(Task, task_id)
        assert row is not None
        row.output = {
            "gates": [
                {"gate": "security", "result": "PASS", "blocking": True},
                {"gate": "coverage", "result": "FAIL", "blocking": False},
            ]
        }
    assert deploy(world, env).status_code == 201
    await drain(world)
    assert history(world)[0]["state"] == "succeeded"


# ---------------------------------------------------------- GitHub Actions --
async def test_a_github_actions_deployment_dispatches_and_follows_the_run(
    world: dict[str, Any],
) -> None:
    remote: Remote = world["remote"]
    c, p, h = world["c"], world["p"], world["h"]
    r = c.post(
        f"/api/v1/projects/{p}/environments",
        json={
            "name": "preview",
            "kind": "github_actions",
            "workflow": "deploy.yml",
            "ref_input": "ref",
        },
        headers=h["owner"],
    )
    env = r.json()
    # Not connected yet: refused with what to set.
    r = deploy(world, env)
    assert r.status_code == 409 and "GitHub repository" in r.json()["error"]["message"]

    c.put(f"/api/v1/projects/{p}/github", json={"repository": "acme/shop"}, headers=h["owner"])
    set_secret(world, "github.token", "ghp_test_token_123456")
    off = deploy(world, env)  # connected but turned off: refused at once
    assert off.status_code == 409 and "turned off" in off.json()["error"]["message"]
    c.put(
        f"/api/v1/projects/{p}/github",
        json={"repository": "acme/shop", "enabled": True},
        headers=h["owner"],
    )
    started = deploy(world, env).json()
    await drain(world)
    done = history(world)[0]
    assert done["id"] == started["id"] and done["state"] == "succeeded", done
    assert done["commit"] == SHA
    assert done["external_url"] == "https://github.com/acme/shop/actions/runs/777"
    [dispatch] = [b for m, path, b in remote.github if path.endswith("/dispatches")]
    assert dispatch == {"ref": "main", "inputs": {"environment": "preview", "ref": "main"}}

    remote.conclusion, remote.polls, remote.dispatched_at = "failure", 0, None
    deploy(world, env)
    await drain(world)
    failed = history(world)[0]
    assert failed["state"] == "failed" and "failure" in failed["detail"]

    deploy(world, env, ref="nope")
    await drain(world)
    assert "not found on GitHub" in history(world)[0]["detail"]


async def test_a_deploy_run_cannot_take_over_another_deployment(world: dict[str, Any]) -> None:
    srv: Server = world["srv"]
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    env = create_env(world)
    set_secret(world, "deploy.staging.webhook", "h" * 32)
    started = deploy(world, env).json()
    params = {"deployment_id": started["id"], "environment_id": env["id"], "ref": "main"}
    hijack = srv.create_run(owner, world["p"], "deploy", params)
    elsewhere = srv.create_run(owner, world["beta"], "deploy", params)
    await drain(world)
    assert srv.run_status(owner, hijack)["state"] == "FAILED"
    assert srv.run_status(owner, elsewhere)["state"] == "FAILED"
    assert history(world)[0]["state"] == "succeeded"
    assert len(world["remote"].hooks) == 1


# ----------------------------------------------------------- public only --
def test_only_public_https_addresses_are_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    for url in (
        "https://127.0.0.1/x",
        "https://10.0.0.1/x",
        "https://[fd00::1]/x",
        "https://169.254.169.254/x",
        "http://example.com/x",
        "https://localhost/x",
        "https://intranet/x",
        "https://printer.local/x",
        "https://[::ffff:127.0.0.1]/x",
    ):
        with pytest.raises(ValueError):
            flow.check_public_url(url)
    assert flow.check_public_url("https://deploy.example.com/x") == "https://deploy.example.com/x"
    assert flow.check_public_url("https://93.184.216.34/x")

    answers = {
        "rebind.example.com": ["93.184.216.34", "10.0.0.5"],
        "ok.example.com": ["93.184.216.34"],
    }

    def fake_resolve(host: str, port: int, *a: Any, **k: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers[host]]

    monkeypatch.setattr(socket, "getaddrinfo", fake_resolve)
    seen: list[httpx.Request] = []

    def inner(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    sync = flow.PublicOnlySyncTransport(httpx.MockTransport(inner))
    with httpx.Client(transport=sync) as client:
        with pytest.raises(httpx.ConnectError, match="non-public"):
            client.get("https://rebind.example.com/")  # one internal address is enough
        with pytest.raises(httpx.ConnectError, match="https"):
            client.get("http://ok.example.com/")
        assert client.get("https://ok.example.com/a").status_code == 200
    [request] = seen
    assert request.url.host == "93.184.216.34"  # connected to the address that was checked
    assert request.headers["Host"] == "ok.example.com"
    assert request.extensions["sni_hostname"] == "ok.example.com"


def test_the_webhook_signature_is_an_hmac_of_the_body() -> None:
    body = b'{"event":"deploy"}'
    assert flow.sign("k", body) == "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
