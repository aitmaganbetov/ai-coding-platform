"""Programmatic Alembic migrations (packaged with the wheel)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from ai_coding.persistence.db import Database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    """Alembic config pointing at the packaged migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade_to_head(db: Database) -> None:
    """Apply all pending migrations."""
    cfg = alembic_config(db.url)
    with db.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def current_revision(db: Database) -> str | None:
    """Revision currently applied to the database (``None`` if empty)."""
    with db.engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    """Latest revision shipped with the package."""
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
