"""B-801 (DAST, authorization): every route of the OpenAPI contract, every method.

Not a list of known endpoints — the routes are read from the application, so a new
endpoint without authentication or project isolation fails here:

* without credentials: 401 (only login and the public probes are open);
* a user of another project: 404 for every object of the victim project, never 2xx,
  never 5xx, and the response never contains the victim's data;
* a garbage or revoked token: 401.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.domain.enums import Role

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
MARKER = "VICTIM_SECRET_MARKER_5e1d"
PUBLIC = {("POST", "/api/v1/auth/login")}  # everything else needs credentials
ADMIN_ONLY = {
    ("GET", "/api/v1/users"),
    ("POST", "/api/v1/users"),
    ("PATCH", "/api/v1/users/{username}"),
    ("PUT", "/api/v1/users/{username}/password"),
    ("GET", "/api/v1/system"),
}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    roots = tmp_path / "repos"
    for name in ("victim", "mine"):
        (roots / name).mkdir(parents=True)
        (roots / name / f"{name}.py").write_text(f'X = "{MARKER if name == "victim" else 1}"\n',
                                                 encoding="utf-8")  # fmt: skip
    srv = Server(ServerSettings(data_dir=tmp_path / "data",
                                database_url=f"sqlite:///{tmp_path / 's.db'}",
                                allowed_roots=(roots.resolve(),),
                                rate_limit_per_minute=100_000))  # fmt: skip
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    srv.create_user("victim", PASSWORD)
    srv.create_user("intruder", PASSWORD)
    admin = srv.login("admin", PASSWORD)
    assert admin is not None
    victim_p = srv.create_project(admin, "victim", path=str(roots.resolve() / "victim"))
    mine_p = srv.create_project(admin, "mine", path=str(roots.resolve() / "mine"))
    srv.set_member(admin, victim_p, "victim", Role.OWNER)
    srv.set_member(admin, mine_p, "intruder", Role.OWNER)
    victim = srv.login("victim", PASSWORD)
    assert victim is not None
    run = srv.create_run(victim, victim_p, "discover", {})
    asyncio.run(Worker(srv, poll_seconds=0.01).run_until_idle(timeout=120))
    artifact = srv.artifacts(victim, run)[0]["id"]
    victim_token, token_id = srv.issue_token(victim, "ci")
    yield {"server": srv, "project": str(victim_p), "run": str(run), "artifact": artifact,
           "token_id": str(token_id), "victim_token": victim_token}  # fmt: skip
    srv.close()


_SPEC: dict[str, Any] = {}


def _routes(client: TestClient) -> list[tuple[str, str]]:
    _SPEC.clear()
    _SPEC.update(client.get("/api/v1/openapi.json").json())
    return [(m.upper(), p) for p, ops in _SPEC["paths"].items() for m in ops]


def _example(schema: dict[str, Any]) -> Any:
    """A minimal valid value for an OpenAPI schema (required properties only)."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _example(_SPEC["components"]["schemas"][name])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"]
            return _example(options[0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: _example(props[k]) for k in schema.get("required", []) if k in props}
    if kind == "string":  # "discover" is a valid run kind; honour minimum lengths
        return "discover".ljust(int(schema.get("minLength", 0)), "x")
    return {"boolean": True, "integer": 1, "number": 1.0, "array": []}.get(str(kind), "x")


def _body(method: str, path: str) -> Any:
    op = _SPEC["paths"][path][method.lower()]
    content = op.get("requestBody", {}).get("content", {}).get("application/json")
    return _example(content["schema"]) if content else None


def _fill(path: str, ids: dict[str, Any]) -> str:
    values = {"project_id": ids["project"], "run_id": ids["run"], "artifact_id": ids["artifact"],
              "token_id": ids["token_id"], "username": "victim"}  # fmt: skip
    # Any other path parameter (a secret's name, an environment…) gets a placeholder: the
    # access checks come before any lookup.
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), "victim"), path)


def _call(client: TestClient, method: str, path: str, ids: dict[str, Any],
          headers: dict[str, str]) -> Any:  # fmt: skip
    body = _body(method, path)
    url = _fill(path, ids)
    if body is not None:
        return client.request(method, url, headers=headers, json=body)
    return client.request(method, url, headers=headers)


def test_every_route_requires_credentials(world: dict[str, Any]) -> None:
    with TestClient(create_app(world["server"])) as c:
        routes = _routes(c)
        assert len(routes) >= 20
        for method, path in routes:
            if (method, path) in PUBLIC:
                continue
            for headers in ({}, {"Authorization": "Bearer aic_" + "x" * 43},
                            {"Authorization": "Basic YWRtaW46eA=="}):  # fmt: skip
                r = _call(c, method, path, world, headers)
                assert r.status_code == 401, (method, path, headers, r.status_code)
                assert MARKER not in r.text


def test_another_projects_objects_are_invisible(world: dict[str, Any]) -> None:
    srv = world["server"]
    intruder = srv.login("intruder", PASSWORD)
    token, _ = srv.issue_token(intruder, "probe")
    h = {"Authorization": f"Bearer {token}"}
    checked = 0
    with TestClient(create_app(srv)) as c:
        missing = c.get(f"/api/v1/runs/{uuid.uuid4()}", headers=h).json()
        for method, path in _routes(c):
            if (method, path) in PUBLIC | ADMIN_ONLY or "{" not in path:
                continue
            r = _call(c, method, path, world, h)
            assert r.status_code in {404, 403}, (method, path, r.status_code, r.text)
            assert r.status_code < 500 and MARKER not in r.text
            scoped = path.startswith(("/api/v1/runs/", "/api/v1/projects/{"))
            if scoped and "members" not in path:
                assert r.status_code == 404, (method, path)  # not a hint that it exists
            checked += 1
        assert c.get(f"/api/v1/runs/{world['run']}", headers=h).json() == missing
        # The admin-only routes refuse a plain user.
        for method, path in ADMIN_ONLY:
            assert _call(c, method, path, world, h).status_code == 403
        listing = c.get(f"/api/v1/audit-events?project_id={world['project']}", headers=h)
        assert listing.status_code == 404
    assert checked >= 12


def test_a_revoked_token_stops_working_at_once(world: dict[str, Any]) -> None:
    h = {"Authorization": f"Bearer {world['victim_token']}"}
    with TestClient(create_app(world["server"])) as c:
        assert c.get("/api/v1/me", headers=h).status_code == 200
        assert c.delete(f"/api/v1/tokens/{world['token_id']}", headers=h).status_code == 204
        assert c.get("/api/v1/me", headers=h).status_code == 401
        assert c.get(f"/api/v1/runs/{world['run']}", headers=h).status_code == 401
