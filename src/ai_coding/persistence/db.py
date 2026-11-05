"""Engine/session factory with safe SQLite defaults (ADR-0005)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL_ENV = "AI_CODING_DATABASE_URL"
STATE_DIR = ".ai-coding"
STATE_DB = "state.db"


def default_database_url(project_root: Path) -> str:
    """``$AI_CODING_DATABASE_URL`` or ``sqlite:///<root>/.ai-coding/state.db``."""
    env = os.environ.get(DATABASE_URL_ENV)
    if env:
        return env
    path = (project_root / STATE_DIR / STATE_DB).resolve()
    return f"sqlite:///{path.as_posix()}"


def ensure_state_dir(project_root: Path) -> Path:
    """Create ``.ai-coding/`` with a self-ignoring ``.gitignore``.

    Local state (database, artifacts) must never be committed, and the platform must
    not edit the user's own ``.gitignore`` to achieve that.
    """
    state = project_root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Local AI Coding Platform state; never commit.\n*\n", encoding="utf-8")
    return state


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


class Database:
    """Owns the engine and session factory for one database."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        parsed = make_url(url)
        self.url = url
        self.is_sqlite = parsed.get_backend_name() == "sqlite"
        if self.is_sqlite and parsed.database not in (None, "", ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if self.is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(url, **kwargs)
        if self.is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def for_project(cls, project_root: Path) -> Database:
        """Database for a project directory (local mode)."""
        ensure_state_dir(project_root)
        return cls(default_database_url(project_root))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session: commit on success, rollback on error."""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Close pooled connections."""
        self.engine.dispose()
