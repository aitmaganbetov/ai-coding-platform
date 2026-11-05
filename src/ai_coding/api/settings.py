"""Server settings from environment variables (12-factor; secrets never in files).

| Variable | Default |
| --- | --- |
| ``AI_CODING_SERVER_DATA_DIR`` | ``~/.local/share/ai-coding-server`` (checkouts, SQLite) |
| ``AI_CODING_SERVER_DATABASE_URL`` | ``sqlite:///<data dir>/server.db`` (PostgreSQL in prod) |
| ``AI_CODING_SERVER_WORKERS`` | ``10`` concurrent runs per worker process (NFR-02) |
| ``AI_CODING_SERVER_ALLOWED_ROOTS`` | empty: projects may only be cloned from Git URLs |
| ``AI_CODING_SERVER_RATE_LIMIT`` | ``120`` requests per minute per principal |
| ``AI_CODING_SERVER_LOGIN_RATE_LIMIT`` | ``10`` login attempts per minute per client |
| ``AI_CODING_SERVER_TRUSTED_PROXIES`` | empty: ``X-Forwarded-For`` is ignored |
| ``AI_CODING_SERVER_METRICS_TOKEN`` | empty: ``/metrics`` requires an authenticated admin |
| ``AI_CODING_SERVER_RETENTION_DAYS`` | ``0``: no automatic purge |
| ``AI_CODING_SERVER_RATE_LIMIT_BACKEND`` | ``auto``: shared on PostgreSQL, else in-process |
| ``AI_CODING_SERVER_DB_POOL_SIZE`` | ``10`` database connections kept per process (PostgreSQL) |
| ``AI_CODING_SERVER_DB_MAX_OVERFLOW`` | ``10`` extra connections per process under load |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "AI_CODING_SERVER_"


def _int(env: Mapping[str, str], name: str, default: int, *, low: int, high: int) -> int:
    raw = env.get(PREFIX + name)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if not low <= value <= high:
        raise ValueError(f"{PREFIX}{name} must be between {low} and {high}")
    return value


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = env.get(PREFIX + name, "").strip().lower() or default
    if value not in allowed:
        raise ValueError(f"{PREFIX}{name} must be one of {', '.join(allowed)}, not {value!r}")
    return value


def _paths(env: Mapping[str, str], name: str) -> tuple[Path, ...]:
    raw = env.get(PREFIX + name, "")
    return tuple(Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip())


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Effective server configuration."""

    data_dir: Path
    database_url: str
    workers: int = 10
    allowed_roots: tuple[Path, ...] = ()
    rate_limit_per_minute: int = 120
    login_rate_limit_per_minute: int = 10
    trusted_proxies: frozenset[str] = field(default_factory=frozenset)
    metrics_token: str | None = None
    retention_days: int = 0
    rate_limit_backend: str = "auto"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    token_ttl_days: int = 30
    agent_token_ttl_hours: int = 24
    max_request_bytes: int = 1_000_000

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        """Settings from ``env`` (default: ``os.environ``)."""
        env = os.environ if env is None else env
        data = Path(env.get(PREFIX + "DATA_DIR") or "~/.local/share/ai-coding-server")
        data = data.expanduser().resolve()
        url = env.get(PREFIX + "DATABASE_URL") or f"sqlite:///{(data / 'server.db').as_posix()}"
        proxies = frozenset(
            p.strip() for p in env.get(PREFIX + "TRUSTED_PROXIES", "").split(",") if p.strip()
        )
        return cls(
            data_dir=data,
            database_url=url,
            workers=_int(env, "WORKERS", 10, low=1, high=256),
            allowed_roots=_paths(env, "ALLOWED_ROOTS"),
            rate_limit_per_minute=_int(env, "RATE_LIMIT", 120, low=1, high=100_000),
            login_rate_limit_per_minute=_int(env, "LOGIN_RATE_LIMIT", 10, low=1, high=10_000),
            trusted_proxies=proxies,
            metrics_token=env.get(PREFIX + "METRICS_TOKEN") or None,
            retention_days=_int(env, "RETENTION_DAYS", 0, low=0, high=36_500),
            rate_limit_backend=_choice(
                env, "RATE_LIMIT_BACKEND", "auto", ("auto", "memory", "database")
            ),
            db_pool_size=_int(env, "DB_POOL_SIZE", 10, low=1, high=200),
            db_max_overflow=_int(env, "DB_MAX_OVERFLOW", 10, low=0, high=200),
        )
