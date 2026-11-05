"""Server job queue and workers (S8, B-703/B-704, NFR-02, AC-04 on the server)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_coding.api.app import create_app
from ai_coding.api.kinds import KINDS
from ai_coding.api.services import Principal, Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import MAX_ATTEMPTS, JobQueue, Worker
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import RunJob
from ai_coding.persistence.types import utcnow
from tests.integration import server_support
from tests.integration.server_support import CRASH_ENV, STATS

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    server_support.register()
    roots = tmp_path / "repos"
    (roots / "alpha").mkdir(parents=True)
    (roots / "alpha" / "main.py").write_text("print(1)\n", encoding="utf-8")
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
    KINDS.pop("probe", None)


@pytest.fixture
def setup(server: Server) -> tuple[Principal, uuid.UUID]:
    server.create_user("admin", PASSWORD, is_admin=True)
    admin = server.login("admin", PASSWORD)
    assert admin is not None
    root = server.settings.allowed_roots[0] / "alpha"
    return admin, server.create_project(admin, "alpha", path=str(root))


async def test_discover_run_executes_and_artifacts_are_downloadable(
    server: Server, setup: tuple[Principal, uuid.UUID]
) -> None:
    admin, alpha = setup
    run = server.create_run(admin, alpha, "discover", {})
    assert await Worker(server, poll_seconds=0.05).run_until_idle() == 1
    status = server.run_status(admin, run)
    assert status["state"] == "COMPLETED", status
    assert [j["state"] for j in status["jobs"]] == ["done"]
    arts = server.artifacts(admin, run)
    assert arts and arts[0]["name"].endswith(".json")
    with TestClient(create_app(server)) as client:
        token, _ = server.issue_token(admin, "t")
        r = client.get(
            f"/api/v1/runs/{run}/artifacts/{arts[0]['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200 and r.headers["Content-Disposition"].startswith("attachment")
        assert r.content and r.headers["X-Content-Type-Options"] == "nosniff"


SLEEP = 2.5


async def test_ten_runs_execute_in_parallel(
    server: Server, setup: tuple[Principal, uuid.UUID], tmp_path: Path
) -> None:
    """NFR-02: at least 10 concurrent runs per worker process."""
    admin, alpha = setup
    marker = tmp_path / "marker.txt"
    # Each run holds its step long enough for a slow CI runner to start all twelve;
    # sequential execution would take 12 x SLEEP.
    runs = [
        server.create_run(admin, alpha, "probe", {"sleep": SLEEP, "marker": str(marker)})
        for _ in range(12)
    ]
    STATS.update(current=0, peak=0)
    worker = Worker(server, concurrency=12, poll_seconds=0.05)
    started = time.monotonic()
    assert await worker.run_until_idle(timeout=120) == 12
    elapsed = time.monotonic() - started
    assert STATS["peak"] >= 10, STATS
    assert elapsed < 12 * SLEEP / 2  # far below sequential execution
    assert {server.run_status(admin, r)["state"] for r in runs} == {"COMPLETED"}


async def test_concurrency_limit_is_respected(
    server: Server, setup: tuple[Principal, uuid.UUID], tmp_path: Path
) -> None:
    admin, alpha = setup
    for _ in range(5):
        server.create_run(admin, alpha, "probe", {"sleep": 0.3, "marker": str(tmp_path / "m")})
    STATS.update(current=0, peak=0)
    assert await Worker(server, concurrency=2, poll_seconds=0.05).run_until_idle() == 5
    assert STATS["peak"] == 2


def test_two_queues_never_take_the_same_job(
    server: Server, setup: tuple[Principal, uuid.UUID], tmp_path: Path
) -> None:
    admin, alpha = setup
    server.create_run(admin, alpha, "probe", {"marker": str(tmp_path / "m")})
    a, b = JobQueue(server, "worker-a"), JobQueue(server, "worker-b")
    first = a.lease_next()
    assert first is not None and b.lease_next() is None
    assert b.renew(first.id) is False and a.renew(first.id) is True
    a.finish(first.id, ok=True)
    with server.session() as s:
        job = s.scalar(select(RunJob))
        assert job is not None and job.state == "done" and job.lease_owner is None


def test_expired_lease_is_taken_over_and_poison_jobs_stop(
    server: Server, setup: tuple[Principal, uuid.UUID], tmp_path: Path
) -> None:
    admin, alpha = setup
    server.create_run(admin, alpha, "probe", {"marker": str(tmp_path / "m")})
    dead = JobQueue(server, "dead", lease_seconds=0.01)
    assert dead.lease_next() is not None
    time.sleep(0.05)
    alive = JobQueue(server, "alive")
    taken = alive.lease_next()
    assert taken is not None and taken.attempts == 2
    with server.session() as s:
        job = s.scalar(select(RunJob))
        assert job is not None
        job.attempts, job.lease_expires_at = MAX_ATTEMPTS, utcnow()
    time.sleep(0.01)
    assert alive.lease_next() is None  # gave up: marked failed, not retried forever
    with server.session() as s:
        job = s.scalar(select(RunJob))
        assert job is not None and job.state == "failed" and "gave up" in (job.error or "")


async def test_killed_worker_process_run_resumes_from_checkpoint(
    server: Server, setup: tuple[Principal, uuid.UUID], tmp_path: Path
) -> None:
    """AC-04 on the server: a real worker process dies in step 2; another resumes."""
    admin, alpha = setup
    marker = tmp_path / "marker.txt"
    run = server.create_run(admin, alpha, "probe", {"steps": 3, "marker": str(marker)})
    env = {**os.environ, CRASH_ENV: "probe.1", "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.integration.server_support",
            server.settings.database_url,
            str(server.settings.data_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 137, proc.stderr[-2000:]
    crashed = server.run_status(admin, run)
    assert crashed["state"] == "RUNNING" and crashed["jobs"][0]["state"] == "leased"
    await asyncio.sleep(1.0)  # the dead worker's job and run leases expire (0.6 s)
    worker = Worker(server, poll_seconds=0.05, lease_seconds=0.6)
    assert await worker.run_until_idle() == 1
    final = server.run_status(admin, run)
    assert final["state"] == "COMPLETED", final
    lines = marker.read_text(encoding="utf-8").split()
    assert lines == ["probe.0", "probe.1", "probe.1", "probe.2"]  # step 0 not repeated
    assert final["jobs"][0]["attempts"] == 2


async def test_approval_resumes_through_the_queue(
    server: Server, setup: tuple[Principal, uuid.UUID], tmp_path: Path
) -> None:
    """A run waiting for approval continues on a worker after the API decision."""
    admin, alpha = setup
    server.create_user("maint", PASSWORD)
    server.set_member(admin, alpha, "maint", Role.MAINTAINER)
    maint = server.login("maint", PASSWORD)
    assert maint is not None
    run = server.create_run(admin, alpha, "probe", {"marker": str(tmp_path / "m"), "approve": True})
    worker = Worker(server, poll_seconds=0.05)
    await worker.run_until_idle()
    waiting = server.run_status(admin, run)
    assert waiting["state"] == "AWAITING_APPROVAL", waiting
    assert waiting["jobs"][0]["state"] == "done"  # nothing occupies a worker while waiting
    assert server.decide(maint, run, approve=True, note="ok")["decided"]
    assert [j["kind"] for j in server.run_status(admin, run)["jobs"]] == ["execute", "resume"]
    await worker.run_until_idle()
    assert server.run_status(admin, run)["state"] == "COMPLETED"
