"""Shared rate limiter (B-807): one budget for every API process, kept in the database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ai_coding.api.app import create_app
from ai_coding.api.limits import RateLimiter, SharedRateLimiter
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.persistence import Database, upgrade_to_head
from ai_coding.persistence.models import RateLimitCounter

T0 = 1_700_000_000.0 - (1_700_000_000.0 % 60)  # the start of a minute


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'l.db'}")
    upgrade_to_head(database)
    yield database
    database.dispose()


def test_processes_share_one_budget(db: Database) -> None:
    a, b = SharedRateLimiter(db, 10, sync_seconds=0), SharedRateLimiter(db, 10, sync_seconds=0)
    allowed = [lim.check("user:1", T0 + 1) is None for lim in (a, b) * 6]
    assert allowed.count(True) == 10 and allowed[-2:] == [False, False]
    wait = a.check("user:1", T0 + 1)
    assert wait is not None and 50 <= wait <= 60
    assert a.check("user:2", T0 + 1) is None  # keys are independent
    with db.session() as s:
        keys = {r.key for r in s.query(RateLimitCounter)}
    assert all(len(k) == 64 and "user" not in k for k in keys)  # only hashes are stored


def test_sliding_window_has_no_double_burst_at_the_boundary(db: Database) -> None:
    lim = SharedRateLimiter(db, 10, sync_seconds=0)
    assert all(lim.check("k", T0 + 59) is None for _ in range(10))
    # 1 s into the next minute the previous minute still weighs ~59/60 of its 10.
    assert lim.check("k", T0 + 61) is not None
    # Half-way through, half of the previous budget is free again.
    assert sum(lim.check("k", T0 + 90) is None for _ in range(10)) == 5  # rejected ones never count
    # Two minutes later everything is free.
    assert lim.check("k", T0 + 181) is None


def test_old_minutes_are_purged(db: Database) -> None:
    lim = SharedRateLimiter(db, 100, sync_seconds=0, purge_every=3)
    for minute in range(5):
        lim.check(f"k{minute}", T0 + 60 * minute)
    lim.check("k", T0 + 300)
    with db.session() as s:
        assert min(r.minute for r in s.query(RateLimitCounter)) >= int(T0 // 60) + 3


def test_batched_sync_bounds_the_overshoot(db: Database) -> None:
    """With a sync every 0.5 s each process may overshoot by what it saw in between."""
    a, b = SharedRateLimiter(db, 20), SharedRateLimiter(db, 20)
    allowed = 0
    for i in range(100):  # 100 requests per second, alternating processes
        now = T0 + 1 + i / 100
        allowed += (a if i % 2 else b).check("user:1", now) is None
    assert 20 <= allowed <= 20 + 2 * 50 * 0.5  # limit + rate x sync interval per process
    a.check("user:1", T0 + 3)
    with db.session() as s:  # the database holds every process's count
        assert s.scalar(select(func.sum(RateLimitCounter.count))) == allowed


def test_database_failure_falls_back_to_the_local_limiter(tmp_path: Path) -> None:
    broken = Database(f"sqlite:///{tmp_path / 'missing' / 'no-tables.db'}")  # no migration
    lim = SharedRateLimiter(broken, 2)
    assert [lim.check("k") for _ in range(3)][:2] == [None, None]
    assert lim.check("k") is not None  # still limited, by this process alone
    broken.dispose()


def test_backend_selection(tmp_path: Path) -> None:
    def server(backend: str) -> Server:
        env = {"AI_CODING_SERVER_DATA_DIR": str(tmp_path / backend),
               "AI_CODING_SERVER_RATE_LIMIT_BACKEND": backend}  # fmt: skip
        srv = Server(ServerSettings.from_env(env))
        srv.migrate()
        return srv

    for backend, kind in (("auto", RateLimiter), ("memory", RateLimiter),
                          ("database", SharedRateLimiter)):  # fmt: skip
        srv = server(backend)
        with TestClient(create_app(srv)) as c:
            assert isinstance(c.app.state.limiter, kind)  # type: ignore[attr-defined]
        srv.close()
    with pytest.raises(ValueError, match="RATE_LIMIT_BACKEND"):
        ServerSettings.from_env({"AI_CODING_SERVER_DATA_DIR": str(tmp_path),
                                 "AI_CODING_SERVER_RATE_LIMIT_BACKEND": "redis"})  # fmt: skip
