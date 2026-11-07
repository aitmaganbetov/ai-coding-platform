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
| ``AI_CODING_SERVER_WORKSPACES`` | ``local``: terminals started by this process; a URL: |
|   | the workspace service (``server workspaces``); ``off`` |
| ``AI_CODING_SERVER_WORKSPACE_SECRET`` | signs tickets between the API and the service |
| ``AI_CODING_SERVER_WORKSPACE_IMAGE`` | ``ai-coding-workspace:local`` (Claude Code, Codex) |
| ``AI_CODING_SERVER_WORKSPACE_EGRESS`` | hosts the terminals may reach (``*``: any) |
| ``AI_CODING_SERVER_SECRET_KEY`` | encrypts projects' secrets at rest (32 bytes, base64 or hex); |
|   | unset: secrets (tokens, API keys) cannot be stored |
| ``AI_CODING_SERVER_SECURITY_CENTER`` | ``off``: the AI Security Center API and page do |
|   | not exist; ``on`` enables them (read-only, docs/security-center.md) |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "AI_CODING_SERVER_"

# What a workspace terminal may reach by default: the AI tools' services and their sign-in
# pages, Git hosting and the package registries. Everything else is refused by the proxy.
DEFAULT_WORKSPACE_EGRESS = (
    "api.anthropic.com", "claude.ai", "*.claude.ai", "console.anthropic.com",
    "platform.claude.com", "*.anthropic.com",
    "chatgpt.com", "*.chatgpt.com", "auth.openai.com", "api.openai.com", "*.openai.com",
    "github.com", "*.github.com", "*.githubusercontent.com", "gitlab.com",
    "registry.npmjs.org", "pypi.org", "files.pythonhosted.org",
)  # fmt: skip


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
    max_upload_bytes: int = 500 * 1024 * 1024  # a project folder uploaded from the Web UI
    project_creators: str = "all"  # "all": every user creates projects; "admins": only them
    workspaces: str = "local"  # "local", "off" or the workspace service's URL
    workspace_secret: str | None = None
    # Encrypts the projects' secrets at rest (AES-256-GCM): 32 bytes, base64 or hex.
    secret_key: str | None = None
    workspace_image: str = "ai-coding-workspace:local"
    workspace_egress: tuple[str, ...] = DEFAULT_WORKSPACE_EGRESS  # ("*",): unrestricted
    workspace_runtime: str | None = None  # e.g. "runsc" (gVisor)
    workspace_memory_mb: int = 4096
    workspace_cpus: int = 2
    workspace_idle_minutes: int = 60
    security_center: bool = False  # opt-in module (docs/security-center.md)

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def workspaces_dir(self) -> Path:
        """Per user and project: a clone of the project the user works in."""
        return self.data_dir / "workspaces"

    @property
    def sources_dir(self) -> Path:
        """Per project and user: committed trees of workspaces that runs examine (the
        audits a chat agent starts, ADR-0038); a run never reads a live workspace."""
        return self.data_dir / "sources"

    @property
    def homes_dir(self) -> Path:
        """Per user: the home of their terminals (their own Claude Code / Codex sign-in)."""
        return self.data_dir / "homes"

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
            max_upload_bytes=_int(env, "MAX_UPLOAD_MB", 500, low=1, high=100_000) * 1024 * 1024,
            project_creators=_choice(env, "PROJECT_CREATORS", "all", ("all", "admins")),
            workspaces=_workspaces(env.get(PREFIX + "WORKSPACES", "")),
            workspace_secret=env.get(PREFIX + "WORKSPACE_SECRET") or None,
            secret_key=env.get(PREFIX + "SECRET_KEY") or None,
            workspace_image=env.get(PREFIX + "WORKSPACE_IMAGE") or "ai-coding-workspace:local",
            workspace_egress=_hosts(env.get(PREFIX + "WORKSPACE_EGRESS")),
            workspace_runtime=env.get(PREFIX + "WORKSPACE_RUNTIME") or None,
            workspace_memory_mb=_int(env, "WORKSPACE_MEMORY_MB", 4096, low=256, high=262_144),
            workspace_cpus=_int(env, "WORKSPACE_CPUS", 2, low=1, high=64),
            workspace_idle_minutes=_int(env, "WORKSPACE_IDLE_MINUTES", 60, low=1, high=10_080),
            security_center=_switch(env, "SECURITY_CENTER"),
        )


def _switch(env: Mapping[str, str], name: str) -> bool:
    """An opt-in feature flag: off unless explicitly on; a typo stops the server instead
    of silently choosing either side."""
    value = env.get(PREFIX + name, "").strip().lower()
    if value in {"", "off", "false", "0", "no"}:
        return False
    if value in {"on", "true", "1", "yes"}:
        return True
    raise ValueError(f"{PREFIX}{name} must be on or off, not {value!r}")


def _workspaces(raw: str) -> str:
    value = raw.strip() or "local"
    if value.lower() in {"local", "off"}:
        return value.lower()
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{PREFIX}WORKSPACES must be local, off or the service URL")
    return value.rstrip("/")


def _hosts(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_WORKSPACE_EGRESS
    hosts = tuple(h.strip() for h in raw.split(",") if h.strip())
    if any(h != "*" and (any(c.isspace() for c in h) or h.startswith("-")) for h in hosts):
        raise ValueError(f"{PREFIX}WORKSPACE_EGRESS: comma-separated host names or *")
    return hosts
