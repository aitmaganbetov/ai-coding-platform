"""The CLI's connection to a Self-Hosted server (ADR-0047).

* **Sign-in** (``ai-coding server login``): the server's URL and an API token of the user
  (Web UI → Аккаунт → Токены), kept in ``~/.config/ai-coding/server.json`` readable by the
  user only. ``AI_CODING_REMOTE_URL`` and ``AI_CODING_REMOTE_TOKEN`` override it (CI).
* **Link** (``ai-coding server link``): which server project a directory is, kept in its
  ``.ai-coding/server.json`` (local state, never committed). ``AI_CODING_REMOTE_PROJECT``
  overrides it.
* **Reports**: when a command ends, every run it changed is reported to the linked project
  (:func:`report_touched`): its summary only (state, verdict, gates, counts of new
  findings by severity, branch, commit). A report never fails the command; a failed one
  can be sent again with ``ai-coding server report``.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from ai_coding import __version__
from ai_coding.cli.console import stderr
from ai_coding.persistence.db import ensure_state_dir
from ai_coding.persistence.models import Run
from ai_coding.security import redact

if TYPE_CHECKING:
    from ai_coding.orchestrator import Orchestrator

URL_ENV = "AI_CODING_REMOTE_URL"
TOKEN_ENV = "AI_CODING_REMOTE_TOKEN"  # noqa: S105 - a variable's name
PROJECT_ENV = "AI_CODING_REMOTE_PROJECT"
LINK_FILE = "server.json"  # in the project's .ai-coding/
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class RemoteError(Exception):
    """The server could not be reached or refused; the message says why (no token)."""


@dataclass(frozen=True, slots=True)
class Credentials:
    url: str
    token: str
    user: str | None = None


@dataclass(frozen=True, slots=True)
class Link:
    project_id: str
    project: str | None = None
    url: str | None = None


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ai-coding"


def credentials_path() -> Path:
    return config_dir() / "server.json"


def normalize_url(url: str) -> str:
    """``http(s)://host[:port][/prefix]`` without a trailing slash, or ValueError."""
    text = url.strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not a server URL: {url!r} (expected http(s)://host[:port])")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("the server URL takes no credentials, query or fragment")
    return text


def plain_http(url: str) -> bool:
    """Whether the token would travel unencrypted to another machine."""
    parts = urlsplit(url)
    return parts.scheme == "http" and (parts.hostname or "") not in _LOCAL_HOSTS


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    with contextlib.suppress(OSError):  # Windows: the profile's ACL protects it
        tmp.chmod(0o600)
    tmp.replace(path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_credentials() -> Credentials | None:
    url, token = os.environ.get(URL_ENV), os.environ.get(TOKEN_ENV)
    if url and token:
        return Credentials(normalize_url(url), token)
    data = _read(credentials_path())
    if not data or not isinstance(data.get("url"), str) or not isinstance(data.get("token"), str):
        return None
    user = data.get("user")
    return Credentials(data["url"], data["token"], user if isinstance(user, str) else None)


def save_credentials(creds: Credentials) -> Path:
    path = credentials_path()
    _write_private(path, {"url": creds.url, "token": creds.token, "user": creds.user})
    return path


def forget_credentials() -> bool:
    try:
        credentials_path().unlink()
    except FileNotFoundError:
        return False
    return True


def link_path(root: Path) -> Path:
    return root / ".ai-coding" / LINK_FILE


def load_link(root: Path) -> Link | None:
    env = os.environ.get(PROJECT_ENV)
    if env:
        return Link(env)
    data = _read(link_path(root))
    if not data or not isinstance(data.get("project_id"), str):
        return None
    name, url = data.get("project"), data.get("url")
    return Link(data["project_id"], name if isinstance(name, str) else None,
                url if isinstance(url, str) else None)  # fmt: skip


def save_link(root: Path, link: Link) -> Path:
    ensure_state_dir(root)  # self-ignoring: never committed
    path = link_path(root)
    _write_private(path, {"project_id": link.project_id, "project": link.project, "url": link.url})
    return path


def forget_link(root: Path) -> bool:
    try:
        link_path(root).unlink()
    except FileNotFoundError:
        return False
    return True


# ------------------------------------------------------------------ client --
class Client:
    """The server's REST API with the user's token."""

    def __init__(self, creds: Credentials, *, transport: httpx.BaseTransport | None = None) -> None:
        self.url = creds.url
        self._http = httpx.Client(
            base_url=f"{creds.url}/api/v1", timeout=TIMEOUT, transport=transport,
            headers={"Authorization": f"Bearer {creds.token}",
                     "User-Agent": f"ai-coding-cli/{__version__}"},
        )  # fmt: skip

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            response = self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise RemoteError(f"{self.url} is not reachable: {type(exc).__name__}") from None
        if response.status_code >= 400:
            message = ""
            with contextlib.suppress(ValueError, AttributeError, TypeError):
                message = str(response.json()["error"]["message"])
            hint = " (sign in again: ai-coding server login)" if response.status_code == 401 else ""
            raise RemoteError(redact(f"{self.url}: HTTP {response.status_code} "
                                     f"{message or response.reason_phrase}{hint}"))  # fmt: skip
        try:
            return response.json()
        except ValueError:
            raise RemoteError(f"{self.url} did not answer with JSON: is it the platform?") from None


# ---------------------------------------------------------------- reports --
def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,  # noqa: S603, S607
                             timeout=5, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _audit_report(orch: Orchestrator, artifacts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    for art in artifacts:
        if art.get("name") == "audit-report.json":
            with contextlib.suppress(Exception):
                data = json.loads(orch.artifacts.get(art["sha256"]).decode("utf-8"))
                return data if isinstance(data, dict) else {}
    return {}


def run_summary(orch: Orchestrator, run_id: uuid.UUID) -> dict[str, Any]:
    """What the server receives about a run: no code, paths or findings."""
    status = orch.status(run_id)
    with orch.db.session() as s:
        run = s.get(Run, run_id)
        started, finished = (run.started_at, run.finished_at) if run else (None, None)
    root = orch.project_root
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    summary: dict[str, Any] = {
        "run_id": str(run_id),
        "machine": socket.gethostname()[:255] or "unknown",
        "cli_version": __version__,
        "mode": str(status.mode)[:20],
        "goal": redact(status.goal or "")[:200],
        "state": status.state.value,
        "branch": branch if branch and branch != "HEAD" else None,
        "commit": _git(root, "rev-parse", "HEAD"),
        "tasks": {str(k): int(v) for k, v in status.summary.counts.items()},
        "error": redact(status.error)[:2000] if status.error else None,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
    }
    report = _audit_report(orch, status.summary.artifacts)
    if report.get("verdict") in ("PASS", "FAIL"):
        summary["verdict"] = report["verdict"]
    summary["gates"] = [
        {"gate": str(g.get("gate"))[:64], "result": str(g.get("result"))[:16],
         "blocking": bool(g.get("blocking")), "reason": str(g.get("reason") or "")[:300]}
        for g in (report.get("gates") or [])[:50] if isinstance(g, dict)
    ]  # fmt: skip
    counts = report.get("summary")
    if isinstance(counts, dict):
        summary["findings"] = {str(k): int(v) for k, v in counts.items() if isinstance(v, int)}
    return summary


def send(client: Client, link: Link, summary: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = client.call("POST", f"/projects/{link.project_id}/local-runs", summary)
    return result


def touched_runs(orch: Orchestrator, since: datetime) -> list[uuid.UUID]:
    """Runs of the local database changed since ``since`` (by the command that ends)."""
    with orch.db.session() as s:
        return list(s.scalars(select(Run.id).where(Run.updated_at >= since)
                              .order_by(Run.created_at)))  # fmt: skip


def report_touched(orch: Orchestrator, since: datetime) -> None:
    """Report the runs the command changed, when this directory is linked and the user is
    signed in. Never raises: a report is a courtesy, the command's result stands."""
    try:
        link, creds = load_link(orch.project_root), load_credentials()
        if link is None or creds is None:
            return
        runs = touched_runs(orch, since)
        if not runs:
            return
        if link.url and creds.url != link.url:
            stderr.print(f"[yellow]not reported:[/] this directory is linked to {link.url}, "
                         f"but the CLI is signed in to {creds.url}")  # fmt: skip
            return
        with Client(creds) as client:
            for run_id in runs:
                try:
                    send(client, link, run_summary(orch, run_id))
                except RemoteError as exc:
                    short = str(run_id)[:8]
                    stderr.print(f"[yellow]run {short} not reported:[/] {exc}. "
                                 f"Send it later: ai-coding server report {short}")  # fmt: skip
                    continue
                stderr.print(f"[dim]run {str(run_id)[:8]} reported to {creds.url} "
                             f"(project {link.project or link.project_id})[/]")  # fmt: skip
    except Exception as exc:  # never fail the user's command for a report
        stderr.print(f"[yellow]the run was not reported to the server:[/] {redact(str(exc))}")
