"""REST API (S8, B-700, B-702): auth, isolation, idempotency, limits, headers, metrics."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


def settings_for(tmp_path: Path, **kw: object) -> ServerSettings:
    roots = tmp_path / "repos"
    roots.mkdir(exist_ok=True)
    base = ServerSettings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'server.db'}",
        allowed_roots=(roots.resolve(),),
        metrics_token="scrape-secret-123",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    srv = Server(settings_for(tmp_path))
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    for name in ("dev", "outsider"):
        srv.create_user(name, PASSWORD)
    yield srv
    srv.close()


@pytest.fixture
def client(server: Server) -> Iterator[TestClient]:
    with TestClient(create_app(server)) as c:
        yield c


def login(client: TestClient, name: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def repo(server: Server, name: str) -> str:
    root = server.settings.allowed_roots[0] / name
    root.mkdir()
    (root / "main.py").write_text("print(1)\n", encoding="utf-8")
    return str(root)


def test_login_auth_and_security_headers(client: TestClient) -> None:
    bad = client.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401 and bad.headers["WWW-Authenticate"] == "Bearer"
    assert bad.json() == {"error": {"code": "unauthorized",
                                    "message": "invalid username or password"}}  # fmt: skip
    anon = client.get("/api/v1/projects")
    assert anon.status_code == 401
    assert client.get("/api/v1/projects", headers={"Authorization": "Bearer aic_x"}).status_code \
        == 401  # fmt: skip
    me = client.get("/api/v1/me", headers=login(client, "admin"))
    assert me.status_code == 200 and me.json()["is_admin"] is True
    for header in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
                   "Referrer-Policy"):  # fmt: skip
        assert header in me.headers
    assert me.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in me.headers  # plain HTTP, no trusted proxy
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    assert client.get("/api/v1/openapi.json").json()["info"]["title"] == "AI Coding Platform API"


def test_hsts_only_behind_a_trusted_proxy(tmp_path: Path) -> None:
    srv = Server(settings_for(tmp_path, trusted_proxies=frozenset({"testclient"})))
    srv.migrate()
    try:
        with TestClient(create_app(srv)) as c:
            r = c.get("/healthz", headers={"X-Forwarded-Proto": "https"})
            assert r.headers["Strict-Transport-Security"].startswith("max-age=")
    finally:
        srv.close()


def test_request_size_and_validation_errors(client: TestClient) -> None:
    headers = login(client, "admin")
    big = client.post("/api/v1/projects", headers={**headers, "Content-Length": "2000000"},
                      content=b"{}")  # fmt: skip
    assert big.status_code == 413
    bad = client.post("/api/v1/projects", headers=headers, json={"name": "x", "evil": 1})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid"


def test_users_projects_members_and_isolation(client: TestClient, server: Server) -> None:
    admin, dev, outsider = (login(client, n) for n in ("admin", "dev", "outsider"))
    denied = client.post("/api/v1/users", headers=dev,
                         json={"username": "x", "password": PASSWORD})  # fmt: skip
    assert denied.status_code == 403
    made = client.post("/api/v1/users", headers=admin,
                       json={"username": "carol", "password": PASSWORD})  # fmt: skip
    assert made.status_code == 201
    p = client.post("/api/v1/projects", headers=admin,
                    json={"name": "alpha", "path": repo(server, "alpha")})  # fmt: skip
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    put = client.put(f"/api/v1/projects/{pid}/members/dev", headers=admin,
                     json={"role": "developer"})  # fmt: skip
    assert {"username": "dev", "role": "developer"} in put.json()["members"]
    assert [x["name"] for x in client.get("/api/v1/projects", headers=dev).json()] == ["alpha"]
    assert client.get("/api/v1/projects", headers=outsider).json() == []
    for path in (f"/api/v1/projects/{pid}", f"/api/v1/projects/{pid}/runs",
                 f"/api/v1/projects/{pid}/findings"):  # fmt: skip
        r = client.get(path, headers=outsider)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    missing = client.get(f"/api/v1/projects/{uuid.uuid4()}", headers=outsider)
    assert missing.json() == client.get(f"/api/v1/projects/{pid}", headers=outsider).json()
    run = client.post(f"/api/v1/projects/{pid}/runs", headers=dev, json={"kind": "discover"})
    assert run.status_code == 201, run.text
    rid = run.json()["id"]
    assert run.json()["state"] == "CREATED" and run.json()["jobs"][0]["state"] == "queued"
    assert client.get(f"/api/v1/runs/{rid}", headers=outsider).status_code == 404
    assert client.get(f"/api/v1/runs/{rid}/artifacts", headers=dev).json() == []
    assert client.post(f"/api/v1/runs/{rid}/approve", headers=dev, json={}).status_code == 403
    cancelled = client.post(f"/api/v1/runs/{rid}/cancel", headers=dev, json={"reason": "test"})
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
    gone = client.delete(f"/api/v1/projects/{pid}/members/dev", headers=admin)
    assert gone.status_code == 204
    assert client.get(f"/api/v1/runs/{rid}", headers=dev).status_code == 404
    events = client.get("/api/v1/audit-events", params={"project_id": pid}, headers=admin).json()
    assert {"run.create", "run.cancel", "project.member"} <= {e["action"] for e in events}


def test_idempotency_key(client: TestClient, server: Server) -> None:
    admin = login(client, "admin")
    body = {"name": "beta", "path": repo(server, "beta")}
    key = {**admin, "Idempotency-Key": "create-beta-1"}
    first = client.post("/api/v1/projects", headers=key, json=body)
    second = client.post("/api/v1/projects", headers=key, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and second.headers["Idempotent-Replayed"] == "true"
    other = client.post("/api/v1/projects", headers=key, json={**body, "name": "gamma"})
    assert other.status_code == 422 and "different request" in other.json()["error"]["message"]
    pid = first.json()["id"]
    run_key = {**admin, "Idempotency-Key": "run-1"}
    r1 = client.post(f"/api/v1/projects/{pid}/runs", headers=run_key, json={"kind": "discover"})
    r2 = client.post(f"/api/v1/projects/{pid}/runs", headers=run_key, json={"kind": "discover"})
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get(f"/api/v1/projects/{pid}/runs", headers=admin).json()) == 1
    bad = client.post("/api/v1/projects", headers={**admin, "Idempotency-Key": "a b"}, json=body)
    assert bad.status_code == 422


def test_tokens_api(client: TestClient, server: Server) -> None:
    admin = login(client, "admin")
    issued = client.post("/api/v1/tokens", headers=admin, json={"name": "ci", "ttl_hours": 2})
    assert issued.status_code == 201
    token = issued.json()["token"]
    ci = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/me", headers=ci).status_code == 200
    rotated = client.post(f"/api/v1/tokens/{issued.json()['token_id']}/rotate", headers=admin)
    assert client.get("/api/v1/me", headers=ci).status_code == 401
    new = {"Authorization": f"Bearer {rotated.json()['token']}"}
    assert client.get("/api/v1/me", headers=new).status_code == 200
    names = {t["name"] for t in client.get("/api/v1/tokens", headers=admin).json()}
    assert {"ci", "login"} <= names
    assert client.delete(f"/api/v1/tokens/{rotated.json()['token_id']}",
                         headers=admin).status_code == 204  # fmt: skip
    assert client.get("/api/v1/me", headers=new).status_code == 401


def test_rate_limits(tmp_path: Path) -> None:
    srv = Server(settings_for(tmp_path, rate_limit_per_minute=3, login_rate_limit_per_minute=2))
    srv.migrate()
    srv.create_user("u", PASSWORD)
    try:
        with TestClient(create_app(srv)) as c:
            creds = {"username": "u", "password": PASSWORD}
            token = c.post("/api/v1/auth/login", json=creds).json()["token"]
            c.post("/api/v1/auth/login", json={"username": "u", "password": "wrong-password"})
            limited = c.post("/api/v1/auth/login", json=creds)
            assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
            h = {"Authorization": f"Bearer {token}"}
            assert [c.get("/api/v1/me", headers=h).status_code for _ in range(4)] == [
                200, 200, 200, 429]  # fmt: skip
    finally:
        srv.close()


def test_metrics_endpoint(client: TestClient, server: Server) -> None:
    assert client.get("/metrics").status_code == 401
    dev = login(client, "dev")
    assert client.get("/metrics", headers=dev).status_code == 401  # not an admin
    client.get("/api/v1/projects", headers=dev)
    scraped = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-123"})
    assert scraped.status_code == 200
    body = scraped.text
    assert 'route="/api/v1/projects",status="200"' in body
    assert "aicoding_http_request_duration_seconds_bucket" in body
    assert "aicoding_worker_active_runs 0" in body
    admin = login(client, "admin")
    assert client.get("/metrics", headers=admin).status_code == 200
