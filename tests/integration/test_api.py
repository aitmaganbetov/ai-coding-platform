"""REST API (S8, B-700, B-702): auth, isolation, idempotency, limits, headers, metrics."""

from __future__ import annotations

import io
import subprocess
import tarfile
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


def test_user_administration(client: TestClient, server: Server) -> None:
    admin, dev = login(client, "admin"), login(client, "dev")
    for method, path, body in (("GET", "/api/v1/users", None),
                               ("PATCH", "/api/v1/users/outsider", {"is_active": False}),
                               ("PUT", "/api/v1/users/outsider/password",
                                {"password": "x" * 12})):  # fmt: skip
        assert client.request(method, path, headers=dev, json=body).status_code == 403
    users = client.get("/api/v1/users", headers=admin).json()
    assert [u["username"] for u in users] == ["admin", "dev", "outsider"]
    assert users[0]["is_admin"] is True and users[1]["is_active"] is True

    # Blocking signs the user out at once and refuses new logins; unblocking restores them.
    blocked = client.patch("/api/v1/users/dev", headers=admin, json={"is_active": False})
    assert blocked.status_code == 200 and blocked.json()["is_active"] is False
    assert client.get("/api/v1/me", headers=dev).status_code == 401
    creds = {"username": "dev", "password": PASSWORD}
    assert client.post("/api/v1/auth/login", json=creds).status_code == 401
    client.patch("/api/v1/users/dev", headers=admin, json={"is_active": True})
    dev = login(client, "dev")
    assert client.get("/api/v1/me", headers=dev).status_code == 200

    promoted = client.patch("/api/v1/users/dev", headers=admin,
                            json={"is_admin": True, "display_name": "  Dev One "})  # fmt: skip
    assert promoted.json()["is_admin"] is True and promoted.json()["display_name"] == "Dev One"
    assert client.get("/api/v1/me", headers=dev).json()["is_admin"] is True
    for change in ({"is_admin": False}, {"is_active": False}):  # never lock oneself out
        own = client.patch("/api/v1/users/admin", headers=admin, json=change)
        assert own.status_code == 409
    missing = client.patch("/api/v1/users/nobody", headers=admin, json={"is_admin": True})
    assert missing.status_code == 404

    new_password = "a brand new password"
    reset = client.put("/api/v1/users/outsider/password", headers=admin,
                       json={"password": new_password})  # fmt: skip
    assert reset.status_code == 204
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "outsider", "password": new_password}
        ).status_code
        == 201
    )
    short = client.put("/api/v1/users/outsider/password", headers=admin, json={"password": "short"})
    assert short.status_code == 422

    events = client.get("/api/v1/audit-events", headers=admin).json()
    actions = {(e["action"], e["outcome"]) for e in events}
    assert {("user.update", "success"), ("user.password", "success"),
            ("access.admin", "denied")} <= actions  # fmt: skip


def test_change_own_password(client: TestClient, server: Server) -> None:
    admin, dev = login(client, "admin"), login(client, "dev")
    wrong = client.put("/api/v1/me/password", headers=dev,
                       json={"current_password": "not it", "new_password": "x" * 12})  # fmt: skip
    # 422, not 401: the UI drops its session on a 401, the token itself is fine.
    assert wrong.status_code == 422 and "current password" in wrong.json()["error"]["message"]
    changed = client.put("/api/v1/me/password", headers=dev,
                         json={"current_password": PASSWORD,
                               "new_password": "my new long password"})  # fmt: skip
    assert changed.status_code == 204
    assert client.get("/api/v1/me", headers=dev).status_code == 200  # the session stays
    old = client.post("/api/v1/auth/login", json={"username": "dev", "password": PASSWORD})
    assert old.status_code == 401
    # /me names the session's token, so the UI can tell it apart in the token list.
    me = client.get("/api/v1/me", headers=dev).json()
    assert me["token_id"] in {t["id"] for t in client.get("/api/v1/tokens", headers=dev).json()}

    # A project-scoped token cannot change the password.
    pid = client.post("/api/v1/projects", headers=admin,
                      json={"name": "pw", "path": repo(server, "pw")}).json()["id"]  # fmt: skip
    client.put(f"/api/v1/projects/{pid}/members/dev", headers=admin, json={"role": "developer"})
    scoped = client.post("/api/v1/tokens", headers=dev,
                         json={"name": "p", "project_id": pid}).json()["token"]  # fmt: skip
    denied = client.put("/api/v1/me/password", headers={"Authorization": f"Bearer {scoped}"},
                        json={"current_password": "my new long password",
                              "new_password": "yet another password"})  # fmt: skip
    assert denied.status_code == 403


def test_project_sources(client: TestClient, server: Server) -> None:
    admin, dev = login(client, "admin"), login(client, "dev")
    root = server.settings.allowed_roots[0]
    repo(server, "alpha")
    (root / "alpha" / ".git").mkdir()
    repo(server, "beta")
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    created = client.post("/api/v1/projects", headers=admin,
                          json={"name": "beta", "path": str(root / "beta")})  # fmt: skip
    assert created.status_code == 201
    sources = client.get("/api/v1/project-sources", headers=admin).json()
    assert sources["git"] is True and sources["upload_max_bytes"] > 0
    (listed,) = sources["roots"]
    assert listed["path"] == str(root) and listed["used"] is True  # it contains "beta"
    alpha = {"name": "alpha", "path": str(root / "alpha"), "git": True, "used": False,
             "project": None}  # fmt: skip
    beta = {"name": "beta", "path": str(root / "beta"), "git": False, "used": True}
    assert listed["directories"] == [alpha, {**beta, "project": "beta"}]
    # Every user sees the sources; another project's name only its members and admins.
    (for_dev,) = client.get("/api/v1/project-sources", headers=dev).json()["roots"]
    assert for_dev["directories"] == [alpha, {**beta, "project": None}]


def test_every_user_creates_projects(client: TestClient, server: Server) -> None:
    dev, outsider = login(client, "dev"), login(client, "outsider")
    assert client.get("/api/v1/me", headers=dev).json()["can_create_projects"] is True
    path = repo(server, "devproj")
    made = client.post("/api/v1/projects", headers=dev, json={"name": "devproj", "path": path})
    assert made.status_code == 201 and made.json()["role"] == "owner"
    # A directory belongs to one project: the same one, a parent or a child are refused,
    # or another user would reach the first project's files through a project of theirs.
    (Path(path) / "sub").mkdir()
    for other in (path, str(Path(path) / "sub"), str(Path(path).parent)):
        r = client.post("/api/v1/projects", headers=outsider, json={"name": "steal", "path": other})
        assert r.status_code == 409, (other, r.text)
    assert client.get("/api/v1/projects", headers=outsider).json() == []
    misuse = client.post("/api/v1/projects", headers=dev,
                         json={"name": "x", "path": path, "git_token": "t"})  # fmt: skip
    assert misuse.status_code == 422
    # A token limited to a role below owner, or to a project, creates nothing.
    capped = client.post("/api/v1/tokens", headers=dev,
                         json={"name": "ro", "max_role": "auditor"}).json()["token"]  # fmt: skip
    ro = {"Authorization": f"Bearer {capped}"}
    assert client.get("/api/v1/me", headers=ro).json()["can_create_projects"] is False
    denied = client.post("/api/v1/projects", headers=ro,
                         json={"name": "ro", "path": repo(server, "ro")})  # fmt: skip
    assert denied.status_code == 403


def test_project_creation_restricted_to_admins(tmp_path: Path) -> None:
    srv = Server(settings_for(tmp_path, project_creators="admins"))
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    srv.create_user("dev", PASSWORD)
    try:
        with TestClient(create_app(srv)) as c:
            dev = login(c, "dev")
            assert c.get("/api/v1/me", headers=dev).json()["can_create_projects"] is False
            assert c.get("/api/v1/project-sources", headers=dev).status_code == 403
            body = {"name": "p", "path": repo(srv, "p")}
            assert c.post("/api/v1/projects", headers=dev, json=body).status_code == 403
            tar_headers = {**dev, "Content-Type": "application/x-tar"}
            upload = c.post("/api/v1/projects/upload", params={"name": "u"},
                            headers=tar_headers, content=_tar({"a.py": b"1"}))  # fmt: skip
            assert upload.status_code == 403
            assert c.post("/api/v1/projects", headers=login(c, "admin"), json=body).status_code \
                == 201  # fmt: skip
    finally:
        srv.close()


def _tar(files: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buffer.getvalue()


def test_upload_a_project_folder(client: TestClient, server: Server) -> None:
    dev = login(client, "dev")
    tar_headers = {**dev, "Content-Type": "application/x-tar"}
    link = tarfile.TarInfo("escape")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    archive = _tar(
        {
            "src/app.py": b"print('hi')\n",
            "Мой файл.txt": "привет".encode(),
            "README.md": b"# demo\n",
            # Never taken from an upload: git config can make git run commands.
            ".git/config": b"[core]\n\tfsmonitor = touch /tmp/pwned\n",
            "vendor/lib/.git": b"gitdir: /etc\n",
            ".ai-coding/state.db": b"local state",
        },
        extra=[link],
    )
    made = client.post("/api/v1/projects/upload", params={"name": "uploaded"},
                       headers=tar_headers, content=archive)  # fmt: skip
    assert made.status_code == 201, made.text
    assert made.json()["role"] == "owner"
    pid = uuid.UUID(made.json()["id"])
    root = server.settings.projects_dir / str(pid)
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (root / "Мой файл.txt").is_file()
    assert not (root / "escape").exists() and not (root / "vendor" / "lib" / ".git").exists()
    assert not (root / ".ai-coding" / "state.db").exists()
    config = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "fsmonitor" not in config  # a fresh repository, not the uploaded one
    log = subprocess.run(["git", "log", "--format=%an|%s", "main"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.splitlines()  # fmt: skip
    assert log == ["dev|Import uploaded uploaded via the Web UI"]
    tracked = subprocess.run(["git", "ls-files"], cwd=root, check=True, capture_output=True,
                             text=True).stdout.split()  # fmt: skip
    assert "src/app.py" in tracked and not any(t.startswith(".ai-coding") for t in tracked)
    events = client.get("/api/v1/audit-events", params={"project_id": str(pid)}, headers=dev)
    assert [e["target"] for e in events.json() if e["action"] == "project.create"] == ["uploaded"]
    # A run works on the uploaded project like on any other.
    run = client.post(f"/api/v1/projects/{pid}/runs", headers=dev, json={"kind": "discover"})
    assert run.status_code == 201, run.text


def test_upload_refusals(tmp_path: Path) -> None:
    srv = Server(settings_for(tmp_path, max_upload_bytes=64 * 1024))  # tars pad to 10 KiB
    srv.migrate()
    srv.create_user("dev", PASSWORD)
    try:
        with TestClient(create_app(srv)) as c:
            h = {**login(c, "dev"), "Content-Type": "application/x-tar"}

            def upload(body: bytes, name: str = "p", **headers: str) -> int:
                r = c.post("/api/v1/projects/upload", params={"name": name},
                           headers={**h, **headers}, content=body)  # fmt: skip
                assert r.status_code >= 400 or r.status_code == 201
                if r.status_code >= 400:
                    assert set(r.json()["error"]) >= {"code", "message"}
                    assert str(tmp_path) not in r.text  # no server paths in errors
                return r.status_code

            assert upload(_tar({"a.py": b"1"}), **{"Content-Type": "application/zip"}) == 415
            assert upload(b"not a tar archive at all" * 10) == 422
            assert upload(_tar({})) == 422  # no files
            assert upload(_tar({"../outside.py": b"x"})) == 422
            assert not (tmp_path / "data" / "outside.py").exists()
            assert upload(_tar({"big.bin": b"x" * 100_000})) == 413  # Content-Length
            chunks = iter([b"x" * 40_000, b"x" * 40_000])
            streamed = c.post("/api/v1/projects/upload", params={"name": "p"}, headers=h,
                              content=chunks)  # fmt: skip
            assert streamed.status_code == 413  # chunked, no Content-Length
            assert upload(_tar({"a.py": b"1"}), name="bad name") == 422
            assert upload(_tar({"a.py": b"1"})) == 201
            assert upload(_tar({"a.py": b"1"})) == 409  # the name is taken
            # Nothing is left behind by the refused uploads.
            projects = srv.settings.projects_dir
            assert len(list(projects.iterdir())) == 1
            assert list((srv.settings.data_dir / "uploads").iterdir()) == []
    finally:
        srv.close()


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
