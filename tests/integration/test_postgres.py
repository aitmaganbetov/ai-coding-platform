"""PostgreSQL profile (S8, B-703): migrations, SKIP LOCKED queue with several workers,
API on PostgreSQL, append-only audit trail. Uses a throwaway container."""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from ai_coding.api.app import create_app
from ai_coding.api.kinds import KINDS
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.persistence import Database, upgrade_to_head
from ai_coding.persistence.migrate import alembic_config, current_revision
from ai_coding.persistence.models import Base, RunJob
from tests.integration import server_support
from tests.integration.conftest import require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]
POSTGRES_IMAGE = (
    "mirror.gcr.io/library/postgres"
    "@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b"
)  # 17-alpine
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    require_images(POSTGRES_IMAGE)
    secret = secrets.token_hex(16)
    name = f"aic-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={secret}",
            "-p",
            "127.0.0.1::5432",
            POSTGRES_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
            )
            .stdout.strip()
            .rsplit(":", 1)[-1]
        )
        url = f"postgresql+psycopg://postgres:{secret}@127.0.0.1:{port}/postgres"
        deadline = time.monotonic() + 60
        while True:
            db = Database(url)
            try:
                with db.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
            finally:
                db.dispose()
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def server(postgres_url: str, tmp_path: Path) -> Iterator[Server]:
    server_support.register()
    db = Database(postgres_url)
    with db.engine.begin() as conn:  # a clean schema per test
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    db.dispose()
    roots = tmp_path / "repos"
    (roots / "alpha").mkdir(parents=True)
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data", database_url=postgres_url, allowed_roots=(roots.resolve(),)
        )
    )
    srv.migrate()
    yield srv
    srv.close()
    KINDS.pop("probe", None)


def test_migrations_match_models_on_postgres(server: Server) -> None:
    with server.db.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    assert current_revision(server.db) == "0005_local_runs"
    command.downgrade(alembic_config(server.db.url), "0001_initial")
    upgrade_to_head(server.db)
    assert current_revision(server.db) == "0005_local_runs"


def test_audit_trail_is_append_only_on_postgres(server: Server) -> None:
    server.create_user("alice", PASSWORD)
    with pytest.raises(DBAPIError, match="append-only"), server.db.engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET outcome = 'success'"))


async def test_three_workers_share_the_queue_without_duplicates(
    server: Server, tmp_path: Path
) -> None:
    server.create_user("admin", PASSWORD, is_admin=True)
    admin = server.login("admin", PASSWORD)
    assert admin is not None
    root = server.settings.allowed_roots[0] / "alpha"
    pid = server.create_project(admin, "alpha", path=str(root))
    marker = tmp_path / "marker.txt"
    runs = [
        server.create_run(admin, pid, "probe", {"sleep": 0.2, "marker": str(marker)})
        for _ in range(24)
    ]
    workers = [Worker(server, concurrency=4, poll_seconds=0.05, owner=f"w{i}") for i in range(3)]
    done = await asyncio.gather(*(w.run_until_idle(timeout=120) for w in workers))
    assert sum(done) == 24 and all(n > 0 for n in done)  # every worker took part
    assert sorted(marker.read_text(encoding="utf-8").split()) == ["probe.0"] * 24  # once each
    with server.session() as s:
        jobs = s.scalars(select(RunJob)).all()
        assert {j.state for j in jobs} == {"done"} and {j.attempts for j in jobs} == {1}
    assert {server.run_status(admin, r)["state"] for r in runs} == {"COMPLETED"}


def test_api_on_postgres(server: Server) -> None:
    server.create_user("admin", PASSWORD, is_admin=True)
    with TestClient(create_app(server)) as client:
        token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).json()["token"]
        h = {"Authorization": f"Bearer {token}", "Idempotency-Key": "pg-1"}
        root = str(server.settings.allowed_roots[0] / "alpha")
        first = client.post("/api/v1/projects", headers=h, json={"name": "alpha", "path": root})
        again = client.post("/api/v1/projects", headers=h, json={"name": "alpha", "path": root})
        assert first.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
        pid = first.json()["id"]
        run = client.post(
            f"/api/v1/projects/{pid}/runs",
            headers={"Authorization": h["Authorization"]},
            json={"kind": "discover"},
        )
        assert run.status_code == 201 and run.json()["state"] == "CREATED"


def test_shared_rate_limit_on_postgres(server: Server, postgres_url: str, tmp_path: Path) -> None:
    """B-807: concurrent increments are atomic and two API instances share one budget."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from ai_coding.api.limits import SharedRateLimiter  # noqa: PLC0415
    from ai_coding.persistence.models import RateLimitCounter  # noqa: PLC0415

    # 40 "processes" add to the same row at the same time: the upsert must not lose any.
    limiters = [SharedRateLimiter(server.db, 1000, sync_seconds=0) for _ in range(40)]
    with ThreadPoolExecutor(20) as pool:
        list(pool.map(lambda lim: lim.check("burst", 1_700_000_000.0), limiters))
    with server.db.session() as s:
        assert s.scalar(select(RateLimitCounter.count)) == 40  # no lost update

    server.create_user("root", PASSWORD, is_admin=True)
    settings = ServerSettings(data_dir=tmp_path / "d2", database_url=postgres_url,
                              rate_limit_per_minute=6)  # fmt: skip
    first, second = Server(settings), Server(settings)  # two API processes, one database
    token, _ = first.issue_token(first.login("root", PASSWORD), "t")  # type: ignore[arg-type]
    h = {"Authorization": f"Bearer {token}"}
    codes = []
    with TestClient(create_app(first)) as a, TestClient(create_app(second)) as b:
        for client in (a, b) * 5:  # requests further apart than the sync interval (0.5 s)
            codes.append(client.get("/api/v1/me", headers=h).status_code)
            time.sleep(0.55)
    # One budget for both processes: each sends its latest count with its next request,
    # so the other one may admit at most one extra request (bound: limit + processes).
    assert 6 <= codes.count(200) <= 6 + 2 and codes[-2:] == [429, 429]
    assert codes.count(200) < 10  # in-process limiters would have admitted all ten
    first.close()
    second.close()
