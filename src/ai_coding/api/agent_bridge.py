"""The platform's orchestrator as tools of the chat agents (ADR-0038).

Claude Code and Codex in a user's workspace container reach the orchestrator through the
``platform`` MCP server of the chat daemon (``ai-coding-agent mcp``): it writes a request
into the workspace's *mailbox*, ``<data>/workspaces/<project>/<user>/mailbox``, a
directory mounted into that container only (at ``/platform``). The API picks requests up
and answers next to them:

* ``<id>.req`` — the request (JSON, at most 64 KB); claimed by renaming it to ``.work``;
* ``<id>.bundle`` — with an audit of the workspace: a git bundle of the branch to check;
* ``<id>.res`` — the answer: ``{"ok": true, ...}`` or ``{"ok": false, "error": ...}``.

Who asks is where the request is, never what it says: the agent acts as its user, with
that user's role in that project only, and only through these operations — it can start
an audit or hand a task to the orchestrator, follow its runs and read their results, but
never approve a run (people decide, in the Web UI) nor touch another project. Every file
of the mailbox is the container's: it is opened without following links, size-checked,
and parsed as data; a bundle is plain data too (see :meth:`LocalGitRepository.fetch_bundle`).

An audit of the workspace examines the committed tree of the branch, exported by the
server from the bundle into ``<data>/sources/<project>/<user>/<commit>`` (no ``.git``, no
hooks): the run never reads the live workspace, and keeps the project's configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ai_coding.api.core import (
    ApiError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    detached_project,
)
from ai_coding.api.kinds import KINDS
from ai_coding.api.rbac import Permission, allowed
from ai_coding.domain.enums import ApprovalStatus, Outcome
from ai_coding.git import LocalGitRepository
from ai_coding.git.errors import GitError
from ai_coding.observability import get_logger
from ai_coding.persistence.models import Finding, Run

if TYPE_CHECKING:
    from ai_coding.api.services import Server

_log = get_logger(__name__)

MAILBOX = "mailbox"
REQUEST_LIMIT = 64 * 1024
STALE_SECONDS = 3600  # unanswered or unread files are removed after this
REQUESTS_PER_MINUTE = 120
RUNS_PER_HOUR = 30
KEEP_SOURCES = 5  # snapshots kept per user and project (and any younger than SOURCE_TTL)
SOURCE_TTL = 6 * 3600
TASK_KINDS = ("develop", "maintain")  # the orchestrator's own agents do the work
_ID = re.compile(r"^[0-9a-f]{16,32}$")
_SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

Request = dict[str, Any]


def _clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else f"{text[:limit]}… [{len(text) - limit} more]"


class AgentBridge:
    """Serves the mailboxes of all workspaces (one loop in each API process)."""

    def __init__(self, server: Server, *, poll_seconds: float = 1.0) -> None:
        self.server = server
        self.poll_seconds = poll_seconds
        self._requests: dict[tuple[uuid.UUID, uuid.UUID], deque[float]] = {}
        self._runs: dict[tuple[uuid.UUID, uuid.UUID], deque[float]] = {}

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.poll)
            except Exception:
                _log.exception("agent bridge: poll failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.poll_seconds)

    # ------------------------------------------------------------ mailboxes --
    def poll(self) -> int:
        """Answer every pending request once; the number answered."""
        answered = 0
        for project_id, user_id, path in self._mailboxes():
            try:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                answered += self._serve(project_id, user_id, fd)
            finally:
                os.close(fd)
        return answered

    def _mailboxes(self) -> Iterator[tuple[uuid.UUID, uuid.UUID, Path]]:
        root = self.server.settings.workspaces_dir
        if not root.is_dir():
            return
        for project_dir in root.iterdir():
            project_id = _uuid(project_dir.name)
            if project_id is None or not project_dir.is_dir():
                continue
            for user_dir in project_dir.iterdir():
                user_id = _uuid(user_dir.name)
                box = user_dir / MAILBOX
                if user_id is not None and box.is_dir() and not box.is_symlink():
                    yield project_id, user_id, box

    def _serve(self, project_id: uuid.UUID, user_id: uuid.UUID, fd: int) -> int:
        answered, now = 0, time.time()
        for name in os.listdir(fd):
            rid, _, ext = name.partition(".")
            if ext == "req" and _ID.match(rid):
                self._handle(project_id, user_id, fd, rid)
                answered += 1
            else:  # answers never read, leftovers of crashes, anything else
                with contextlib.suppress(OSError):
                    if (
                        now - os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime
                        > STALE_SECONDS
                    ):
                        os.unlink(name, dir_fd=fd)
        return answered

    def _handle(self, project_id: uuid.UUID, user_id: uuid.UUID, fd: int, rid: str) -> None:
        try:  # claimed: another API process will not answer it too
            os.rename(f"{rid}.req", f"{rid}.work", src_dir_fd=fd, dst_dir_fd=fd)
        except FileNotFoundError:
            return
        try:
            request = _read_json(fd, f"{rid}.work")
            response = {"ok": True, **self.dispatch(project_id, user_id, request, fd=fd, rid=rid)}
        except ApiError as exc:
            response = {"ok": False, "error": exc.message}
        except (ValueError, OSError) as exc:
            response = {"ok": False, "error": f"bad request: {_clip(exc, 300)}"}
        except Exception:
            _log.exception("agent bridge: request failed", project=str(project_id))
            response = {"ok": False, "error": "internal error of the platform"}
        finally:
            for name in (f"{rid}.work", f"{rid}.bundle"):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=fd)
        _write_json(fd, rid, response)

    # ----------------------------------------------------------- operations --
    def dispatch(
        self,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        request: Request,
        *,
        fd: int | None = None,
        rid: str = "",
    ) -> dict[str, Any]:
        """One request of the agent of ``user_id`` in ``project_id`` (its mailbox)."""
        _throttle(self._requests, (project_id, user_id), REQUESTS_PER_MINUTE, 60)
        principal = self.server.principal_of(user_id)
        with self.server.session() as s:  # the workspace's own right, re-checked each time
            self.server.require(s, principal, project_id, Permission.WORKSPACE_USE)
        handlers: dict[str, Callable[[Principal, uuid.UUID, Request], dict[str, Any]]] = {
            "info": self.info,
            "audit": lambda p, pid, req: self.audit(p, pid, req, fd=fd, rid=rid),
            "start_task": self.start_task,
            "status": self.status,
            "report": self.report,
            "cancel": self.cancel,
        }
        handler = handlers.get(str(request.get("op")))
        if handler is None:
            raise InvalidError(f"unknown operation (known: {', '.join(handlers)})")
        return handler(principal, project_id, request)

    def info(self, principal: Principal, project_id: uuid.UUID, _req: Request) -> dict[str, Any]:
        with self.server.session() as s:
            role = self.server.require(s, principal, project_id, Permission.PROJECT_VIEW)
            project = detached_project(s, project_id)
        _root, config = self.server.project_config(project)
        return {
            "project": project.name,
            "user": principal.username,
            "run_kinds": sorted(k for k, spec in KINDS.items() if allowed(role, spec.permission)),
            "ai": {"enabled": config.ai.enabled, "provider": config.ai.provider.value},
            "url": f"#/projects/{project_id}",
        }

    def audit(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        req: Request,
        *,
        fd: int | None,
        rid: str,
    ) -> dict[str, Any]:
        tools = req.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise InvalidError("tools: a list of analyser names")
        params = {"tools": tools, "strict": bool(req.get("strict", False))}
        source = req.get("source", "workspace")
        if source not in ("workspace", "project"):
            raise InvalidError("source: 'workspace' or 'project'")
        if source == "project":
            self._throttle_runs(project_id, principal)
            run_id = self.server.create_run(principal, project_id, "audit", params,
                                            goal="audit: the project")  # fmt: skip
            result: dict[str, Any] = {"source": "project"}
        else:
            if fd is None:
                raise InvalidError("an audit of the workspace needs its bundle")
            branch = str(req.get("branch") or "")
            bundle = self._take_bundle(fd, rid)
            try:
                self._throttle_runs(project_id, principal)
                path, commit = self.snapshot(project_id, principal.user_id, bundle, branch)
            finally:
                bundle.unlink(missing_ok=True)
            label = f"workspace of {principal.username}: {branch} @ {commit[:10]}"
            run_id = self.server.create_run(principal, project_id, "audit", params,
                                            source=path, goal=f"audit: {label}")  # fmt: skip
            result = {"source": "workspace", "branch": branch, "commit": commit}
        self._record(principal, project_id, run_id, "audit")
        return {"run_id": str(run_id), "url": f"#/runs/{run_id}", **result}

    def start_task(
        self, principal: Principal, project_id: uuid.UUID, req: Request
    ) -> dict[str, Any]:
        kind, params = req.get("kind"), req.get("params") or {}
        if kind not in TASK_KINDS:
            raise InvalidError(f"kind: one of {', '.join(TASK_KINDS)}")
        if not isinstance(params, dict):
            raise InvalidError("params: an object")
        with self.server.session() as s:
            project = detached_project(s, project_id)
        _root, config = self.server.project_config(project)
        if not config.ai.enabled:
            raise InvalidError(
                "the project has no AI provider for the orchestrator's agents (.ai-coding.yml: "
                "ai.enabled, ai.provider, ai.model): it cannot take this task; do it in the "
                "chat, or ask a maintainer to configure the provider"
            )
        self._throttle_runs(project_id, principal)
        run_id = self.server.create_run(principal, project_id, str(kind), params)
        self._record(principal, project_id, run_id, str(kind))
        return {"run_id": str(run_id), "url": f"#/runs/{run_id}"}

    def status(self, principal: Principal, project_id: uuid.UUID, req: Request) -> dict[str, Any]:
        run_id = self._run_of(project_id, req)
        status = self.server.run_status(principal, run_id)
        tasks = [{"key": t.get("key"), "status": t.get("status"),
                  **({"error": _clip(t["error"], 500)} if t.get("error") else {})}
                 for t in status.get("tasks", [])]  # fmt: skip
        pending = [a for a in status.get("approvals", [])
                   if a.get("status") == ApprovalStatus.PENDING.value]  # fmt: skip
        approvals = [{"operation": a.get("operation"), "reason": _clip(a.get("reason"), 300)}
                     for a in pending]  # fmt: skip
        return {
            "run_id": str(run_id),
            "state": status.get("state"),
            "goal": status.get("goal"),
            "error": _clip(status.get("error"), 1000) if status.get("error") else None,
            "tasks": tasks,
            "pending_approvals": approvals,
            "summary": status.get("summary"),
            "url": f"#/runs/{run_id}",
        }

    def report(self, principal: Principal, project_id: uuid.UUID, req: Request) -> dict[str, Any]:
        run_id = self._run_of(project_id, req)
        status = self.server.run_status(principal, run_id)
        with self.server.session() as s:
            self.server.require(s, principal, project_id, Permission.FINDINGS_VIEW)
            rows = list(s.scalars(select(Finding).where(Finding.run_id == run_id)))
        rows.sort(
            key=lambda f: (_SEVERITY.get(f.severity.lower(), 9), f.file_path or "", f.line or 0)
        )
        counts: dict[str, int] = {}
        for f in rows:
            counts[f.severity.lower()] = counts.get(f.severity.lower(), 0) + 1
        findings = [
            {"severity": f.severity, "source": f.source, "rule": f.rule_id,
             "title": _clip(f.title, 300), "file": f.file_path, "line": f.line,
             "status": f.status, "message": _clip(f.message, 500)}
            for f in rows[:60]
        ]  # fmt: skip
        markdown = ""
        for art in self.server.artifacts(principal, run_id):
            if str(art["name"]).endswith(".md") or art["media_type"] == "text/markdown":
                artifact = uuid.UUID(art["id"])
                _name, _media, data = self.server.artifact_content(principal, run_id, artifact)
                markdown = _clip(data.decode("utf-8", errors="replace"), 15000)
        return {
            "run_id": str(run_id),
            "state": status.get("state"),
            "summary": status.get("summary"),
            "findings_by_severity": counts,
            "findings": findings,
            "findings_shown": len(findings),
            "report_markdown": markdown,
            "url": f"#/runs/{run_id}",
        }

    def cancel(self, principal: Principal, project_id: uuid.UUID, req: Request) -> dict[str, Any]:
        run_id = self._run_of(project_id, req)
        with self.server.session() as s:
            run = s.get(Run, run_id)
            if run is None or run.requested_by != principal.username:
                raise ForbiddenError("the chat agent cancels only runs of its own user")
        result = self.server.cancel(principal, run_id, "cancelled from the chat")
        self._record(principal, project_id, run_id, "cancel")
        return {"run_id": str(run_id), **result}

    # -------------------------------------------------------------- helpers --
    def _throttle_runs(self, project_id: uuid.UUID, principal: Principal) -> None:
        """Runs cost resources: a bounded number per user and project and hour."""
        _throttle(self._runs, (project_id, principal.user_id), RUNS_PER_HOUR, 3600)

    def _run_of(self, project_id: uuid.UUID, req: Request) -> uuid.UUID:
        """A run of this project (the agent sees no other project's runs)."""
        run_id = _uuid(str(req.get("run_id") or "").strip())
        if run_id is not None:
            with self.server.session() as s:
                run = s.get(Run, run_id)
                if run is not None and run.project_id == project_id:
                    return run_id
        raise NotFoundError("no such run in this project")

    def _record(
        self, principal: Principal, project_id: uuid.UUID, run_id: uuid.UUID, op: str
    ) -> None:
        with self.server.session() as s:
            self.server.record(s, principal.username, "workspace.agent", Outcome.SUCCESS,
                               project_id=project_id, run_id=run_id, target=op,
                               details={"via": "chat"})  # fmt: skip

    def _take_bundle(self, fd: int, rid: str) -> Path:
        """The request's bundle, copied out of the mailbox (size-checked, no links)."""
        try:
            src = os.open(f"{rid}.bundle", _READ, dir_fd=fd)
        except FileNotFoundError:
            raise InvalidError("the request has no bundle of the workspace") from None
        except OSError:
            raise InvalidError("the bundle is not a regular file") from None
        limit = self.server.settings.max_upload_bytes
        uploads = self.server.settings.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        with (
            os.fdopen(src, "rb") as reader,
            tempfile.NamedTemporaryFile(dir=uploads, suffix=".bundle", delete=False) as out,
        ):
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                Path(out.name).unlink(missing_ok=True)
                raise InvalidError("the bundle is not a regular file")
            copied = 0
            while chunk := reader.read(1 << 20):
                copied += len(chunk)
                if copied > limit:
                    out.close()
                    Path(out.name).unlink(missing_ok=True)
                    raise InvalidError("the workspace is larger than the upload limit")
                out.write(chunk)
        return Path(out.name)

    def snapshot(
        self, project_id: uuid.UUID, user_id: uuid.UUID, bundle: Path, branch: str
    ) -> tuple[Path, str]:
        """Export the committed tree of ``branch`` of ``bundle`` for a run: its path and
        commit. The same commit is exported once."""
        base = self.server.settings.sources_dir / str(project_id) / str(user_id)
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix=".import-") as scratch:
            try:
                repo = LocalGitRepository.init(Path(scratch) / "git")
                commit = repo.fetch_bundle(bundle, branch, "source")
                dest = base / commit
                if not dest.is_dir():
                    tree = Path(scratch) / "tree"
                    repo.export(commit, tree)
                    try:
                        tree.rename(dest)
                    except OSError:  # exported meanwhile by another API process
                        if not dest.is_dir():
                            raise
            except (GitError, ValueError) as exc:
                raise InvalidError(
                    f"could not read the workspace's branch: {_clip(exc, 300)}"
                ) from exc
        os.utime(dest)  # most recently used
        _prune(base, keep=dest)
        return dest, commit


def _uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _throttle(
    store: dict[tuple[uuid.UUID, uuid.UUID], deque[float]],
    key: tuple[uuid.UUID, uuid.UUID],
    limit: int,
    window: float,
) -> None:
    now = time.monotonic()
    times = store.setdefault(key, deque())
    while times and now - times[0] > window:
        times.popleft()
    if len(times) >= limit:
        raise InvalidError("too many requests from the chat agent: try again later")
    times.append(now)


def _read_json(fd: int, name: str) -> Request:
    handle = os.open(name, _READ, dir_fd=fd)
    with os.fdopen(handle, "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > REQUEST_LIMIT:
            raise ValueError("request too large")
        data = json.loads(reader.read(REQUEST_LIMIT + 1))
    if not isinstance(data, dict):
        raise ValueError("a request is a JSON object")  # noqa: TRY004 - answered as a bad request
    return data


def _write_json(fd: int, rid: str, response: dict[str, Any]) -> None:
    """Write ``<rid>.res`` atomically, never through a link the container planted."""
    tmp = f".{rid}.res.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp, dir_fd=fd)
    try:
        handle = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        with os.fdopen(handle, "wb") as out:
            out.write(json.dumps(response, ensure_ascii=False, default=str).encode())
        os.rename(tmp, f"{rid}.res", src_dir_fd=fd, dst_dir_fd=fd)
    except OSError:
        _log.warning("agent bridge: could not answer", request=rid)


def _prune(base: Path, *, keep: Path) -> None:
    """Old snapshots go; recent ones may still be examined by a run."""
    now = time.time()
    dirs = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and p != keep]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    kept = 1  # ``keep``
    for path in dirs:
        age = now - path.stat().st_mtime
        if path.name.startswith("."):  # an import a crash left behind
            remove = path.name.startswith(".import-") and age > STALE_SECONDS
        else:
            kept += 1
            remove = kept > KEEP_SOURCES and age > SOURCE_TTL
        if remove:
            shutil.rmtree(path, ignore_errors=True)


__all__ = ["MAILBOX", "AgentBridge"]
