"""Persistence: migrations, schema drift, append-only audit, UTC, UUIDv7, state dir."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from ai_coding.persistence import Database, default_database_url, upgrade_to_head
from ai_coding.persistence.db import DATABASE_URL_ENV, ensure_state_dir
from ai_coding.persistence.migrate import alembic_config, current_revision, head_revision
from ai_coding.persistence.models import (
    AppendOnlyViolationError,
    AuditEvent,
    Base,
    Project,
    Run,
    Task,
)
from ai_coding.persistence.types import utcnow, uuid7

TZ_ENTITIES = {
    "users", "projects", "repositories", "runs", "tasks", "task_dependencies", "agents",
    "agent_executions", "approvals", "artifacts", "findings", "policies", "model_usage",
    "audit_events", "checkpoints",
}  # fmt: skip


def test_all_tz_entities_exist(db: Database) -> None:
    tables = set(inspect(db.engine).get_table_names())
    assert TZ_ENTITIES <= tables
    assert {"run_events", "effects", "project_members"} <= tables


def test_migration_matches_models(db: Database) -> None:
    with db.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    assert current_revision(db) == head_revision() == "0002_server"


def test_upgrade_is_idempotent_and_downgrade_works(db: Database) -> None:
    upgrade_to_head(db)
    command.downgrade(alembic_config(db.url), "base")
    assert current_revision(db) is None
    assert "runs" not in inspect(db.engine).get_table_names()
    upgrade_to_head(db)
    assert current_revision(db) == "0002_server"


def test_sqlite_pragmas(db: Database) -> None:
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_audit_events_append_only_orm(db: Database) -> None:
    with db.session() as s:
        s.add(AuditEvent(actor="a", action="x", outcome="success"))
    with pytest.raises(AppendOnlyViolationError), db.session() as s:
        event = s.query(AuditEvent).one()
        event.actor = "b"
    with pytest.raises(AppendOnlyViolationError), db.session() as s:
        s.delete(s.query(AuditEvent).one())


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_events SET actor = 'x'", "DELETE FROM audit_events"]
)
def test_audit_events_append_only_sql_trigger(db: Database, statement: str) -> None:
    with db.session() as s:
        s.add(AuditEvent(actor="a", action="x", outcome="success"))
    with pytest.raises(DBAPIError, match="append-only"), db.engine.begin() as conn:
        conn.execute(text(statement))


def test_datetimes_are_utc_aware(db: Database) -> None:
    with db.session() as s:
        p = Project(name="p")
        s.add(p)
        s.flush()
        pid = p.id
    with db.session() as s:
        loaded = s.get(Project, pid)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_naive_datetime_rejected(db: Database) -> None:
    with pytest.raises(Exception, match="naive datetime"), db.session() as s:
        s.add(Project(name="p", deleted_at=datetime(2026, 1, 1)))


def test_optimistic_locking_on_tasks(db: Database) -> None:
    with db.session() as s:
        p = Project(name="p")
        s.add(p)
        s.flush()
        r = Run(project_id=p.id, mode="audit", state="CREATED", requested_by="t")
        s.add(r)
        s.flush()
        t = Task(
            run_id=r.id, key="a", name="a", phase="execution", capability="c", status="PENDING"
        )
        s.add(t)
        s.flush()
        tid = t.id
    s1, s2 = db._sessions(), db._sessions()
    try:
        t1, t2 = s1.get(Task, tid), s2.get(Task, tid)
        assert t1 is not None and t2 is not None
        t1.status = "RUNNING"
        s1.commit()
        t2.status = "FAILED"
        with pytest.raises(StaleDataError):
            s2.flush()
    finally:
        s1.close()
        s2.rollback()
        s2.close()


def test_uuid7_is_versioned_and_time_ordered() -> None:
    ids = [uuid7() for _ in range(200)]
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)
    assert len(set(ids)) == 200
    timestamps = [i.int >> 80 for i in ids]
    assert timestamps == sorted(timestamps)
    assert abs((timestamps[0] / 1000) - utcnow().timestamp()) < 5


def test_default_database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = default_database_url(tmp_path)
    assert url.startswith("sqlite:///") and url.endswith(".ai-coding/state.db")
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql+psycopg://u@h/db")
    assert default_database_url(tmp_path) == "postgresql+psycopg://u@h/db"


def test_state_dir_is_self_ignoring(tmp_path: Path) -> None:
    state = ensure_state_dir(tmp_path)
    assert (state / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"
    ensure_state_dir(tmp_path)  # idempotent
    db = Database.for_project(tmp_path)
    upgrade_to_head(db)
    assert (tmp_path / ".ai-coding" / "state.db").is_file()
    db.dispose()


def test_session_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.session() as s:
        s.add(Project(name="rolled-back"))
        s.flush()
        raise RuntimeError("boom")
    with db.session() as s:
        assert s.query(Project).count() == 0


def test_in_memory_database() -> None:
    mem = Database("sqlite://")
    upgrade_to_head(mem)
    assert current_revision(mem) == "0002_server"
    mem.dispose()
