"""AC-08 (S8): a self-hosted server with two projects and several users confirms RBAC
and that no context leaks between projects — through the REST API, the workers'
artifacts, scoped tokens, exports and the audit trail."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
MARKER_A = "ALPHA_ONLY_MARKER_7f3a"  # content that exists only in project alpha
MARKER_B = "BETA_ONLY_MARKER_19c2"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    roots = tmp_path / "repos"
    for name, marker in (("alpha", MARKER_A), ("beta", MARKER_B)):
        (roots / name / "src").mkdir(parents=True)
        (roots / name / "src" / f"{marker.lower()}.py").write_text(
            f'"""{marker}"""\nVALUE = "{marker}"\n', encoding="utf-8"
        )
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'server.db'}",
            allowed_roots=(roots.resolve(),),
        )
    )
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    for name in ("alice", "bob", "carol", "dave"):
        srv.create_user(name, PASSWORD)
    yield srv
    srv.close()


def login(client: TestClient, name: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Scene:
    """Two projects, five users: alice/bob developers of alpha/beta, carol auditor of
    alpha, dave maintainer of both; one discovery run per project."""

    def __init__(self, c: TestClient, roots: Path) -> None:
        self.c = c
        self.admin = login(c, "admin")
        ids = {}
        for name in ("alpha", "beta"):
            r = c.post(
                "/api/v1/projects",
                headers=self.admin,
                json={"name": name, "path": str(roots / name)},
            )
            assert r.status_code == 201, r.text
            ids[name] = r.json()["id"]
        self.alpha, self.beta = ids["alpha"], ids["beta"]
        for project, user, role in (
            (self.alpha, "alice", "developer"),
            (self.beta, "bob", "developer"),
            (self.alpha, "carol", "auditor"),
            (self.alpha, "dave", "maintainer"),
            (self.beta, "dave", "maintainer"),
        ):
            r = c.put(
                f"/api/v1/projects/{project}/members/{user}",
                headers=self.admin,
                json={"role": role},
            )
            assert r.status_code == 200, r.text
        self.alice, self.bob, self.carol, self.dave = (
            login(c, n) for n in ("alice", "bob", "carol", "dave")
        )
        self.run_a = self.start(self.alpha, self.alice)
        self.run_b = self.start(self.beta, self.bob)

    def start(self, project: str, h: dict[str, str]) -> str:
        r = self.c.post(f"/api/v1/projects/{project}/runs", headers=h, json={"kind": "discover"})
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    def names(self, h: dict[str, str]) -> list[str]:
        return [p["name"] for p in self.c.get("/api/v1/projects", headers=h).json()]

    def artifact_text(self, h: dict[str, str], run: str) -> str:
        arts = self.c.get(f"/api/v1/runs/{run}/artifacts", headers=h).json()
        assert arts, "the discovery run produced artifacts"
        return "".join(
            self.c.get(f"/api/v1/runs/{run}/artifacts/{a['id']}", headers=h).text for a in arts
        )


def check_artifacts_isolated(s: Scene) -> None:
    text_a, text_b = s.artifact_text(s.alice, s.run_a), s.artifact_text(s.bob, s.run_b)
    assert MARKER_A.lower() in text_a and MARKER_B.lower() not in text_a
    assert MARKER_B.lower() in text_b and MARKER_A.lower() not in text_b


def check_cross_project_is_404(s: Scene) -> None:
    c = s.c
    art_b = c.get(f"/api/v1/runs/{s.run_b}/artifacts", headers=s.bob).json()[0]["id"]
    for path in (
        f"/api/v1/projects/{s.beta}",
        f"/api/v1/projects/{s.beta}/runs",
        f"/api/v1/projects/{s.beta}/findings",
        f"/api/v1/runs/{s.run_b}",
        f"/api/v1/runs/{s.run_b}/artifacts",
        f"/api/v1/runs/{s.run_b}/artifacts/{art_b}",
        f"/api/v1/audit-events?project_id={s.beta}",
    ):
        r = c.get(path, headers=s.alice)
        assert r.status_code == 404, (path, r.status_code)
        assert MARKER_B not in r.text
    # Indistinguishable from a missing object.
    missing = c.get(f"/api/v1/runs/{uuid.uuid4()}", headers=s.alice)
    assert c.get(f"/api/v1/runs/{s.run_b}", headers=s.alice).json() == missing.json()
    r = c.post(f"/api/v1/projects/{s.beta}/runs", headers=s.alice, json={"kind": "discover"})
    assert r.status_code == 404
    assert c.post(f"/api/v1/runs/{s.run_b}/cancel", headers=s.alice).status_code == 404


def check_roles(s: Scene) -> None:
    c = s.c
    # auditor: read-only
    r = c.post(f"/api/v1/projects/{s.alpha}/runs", headers=s.carol, json={"kind": "discover"})
    assert r.status_code == 403
    # developers do not read the audit trail
    r = c.get(f"/api/v1/audit-events?project_id={s.alpha}", headers=s.alice)
    assert r.status_code == 403
    # only the owner exports or manages members (no self-service escalation)
    assert c.get(f"/api/v1/projects/{s.alpha}/export", headers=s.dave).status_code == 403
    r = c.put(f"/api/v1/projects/{s.alpha}/members/bob", headers=s.dave, json={"role": "owner"})
    assert r.status_code == 403


def check_scoped_token(s: Scene) -> None:
    c = s.c
    r = c.post("/api/v1/tokens", headers=s.dave, json={"name": "alpha-ci", "project_id": s.alpha})
    scoped = bearer(r.json()["token"])
    assert s.names(scoped) == ["alpha"]
    assert c.get(f"/api/v1/runs/{s.run_b}", headers=scoped).status_code == 404
    assert c.get(f"/api/v1/runs/{s.run_b}", headers=s.dave).status_code == 200


def check_export(s: Scene) -> None:
    content = s.c.get(f"/api/v1/projects/{s.alpha}/export", headers=s.admin).content
    export = zipfile.ZipFile(io.BytesIO(content))
    dumped = "".join(export.read(n).decode("utf-8", "replace") for n in export.namelist())
    assert s.run_a in dumped and s.run_b not in dumped and MARKER_B.lower() not in dumped


def check_audit(s: Scene) -> None:
    c = s.c
    events = c.get(f"/api/v1/audit-events?project_id={s.alpha}", headers=s.carol).json()
    assert {e["project_id"] for e in events} == {s.alpha}
    actions = {(e["actor"], e["action"], e["outcome"]) for e in events}
    assert ("carol", "access.run.create", "denied") in actions
    assert ("alice", "access.audit.view", "denied") in actions
    assert ("dave", "access.members.manage", "denied") in actions
    assert not any(e["run_id"] == s.run_b for e in events)
    beta_events = c.get(f"/api/v1/audit-events?project_id={s.beta}", headers=s.dave).json()
    assert beta_events and {e["project_id"] for e in beta_events} == {s.beta}
    assert all(e["run_id"] != s.run_a for e in beta_events)
    assert MARKER_A not in json.dumps(beta_events)
    # Probes of a foreign project are recorded as server-level events for the admins.
    server_events = c.get("/api/v1/audit-events", headers=s.admin).json()
    probes = [
        e
        for e in server_events
        if e["actor"] == "alice" and e["outcome"] == "denied" and e["project_id"] is None
    ]
    assert {e["target"] for e in probes} == {f"project:{s.beta}"}
    assert {"access.run.view", "access.run.create", "access.run.cancel_own"} <= {
        e["action"] for e in probes
    }


async def test_two_projects_rbac_and_no_context_leak(server: Server) -> None:
    with TestClient(create_app(server)) as c:
        s = Scene(c, server.settings.allowed_roots[0])
        # Each user sees exactly their projects.
        assert s.names(s.alice) == ["alpha"] and s.names(s.bob) == ["beta"]
        assert s.names(s.carol) == ["alpha"] and s.names(s.dave) == ["alpha", "beta"]
        assert await Worker(server, poll_seconds=0.05).run_until_idle() == 2
        check_artifacts_isolated(s)
        check_cross_project_is_404(s)
        check_roles(s)
        check_scoped_token(s)
        check_export(s)
        check_audit(s)
