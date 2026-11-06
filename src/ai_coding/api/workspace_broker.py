"""Workspace terminals (ADR-0036): a long-lived container per user and project with the
official Claude Code and Codex CLIs, reached from the browser over a WebSocket.

* Each user signs in to their own Claude / ChatGPT subscription *inside* the official
  CLIs; the sign-in lives in their own home (``<data>/homes/<user>``), mounted only
  into their own containers. The platform never sees it nor the prompts.
* The container sees the user's workspace (``/workspace``, their clone of the project),
  their home and, read-only, the project repository (``/project``, the ``project``
  remote). It runs without capabilities, as the server's uid, with limits, and reaches
  only the hosts of ``AI_CODING_SERVER_WORKSPACE_EGRESS`` through an allowlist proxy.
* Git commands on a workspace run *in that container*: the repository's configuration is
  the user's (a ``core.fsmonitor`` or a filter would run commands), so the server never
  runs git on it. Changes reach the project as a git bundle (plain data).
* The shell runs in tmux: closing the tab detaches; Claude Code keeps working. A
  container without attached terminal for ``AI_CODING_SERVER_WORKSPACE_IDLE_MINUTES`` is
  removed (the workspace and the home stay).

:class:`LocalBroker` drives Docker from this process. The API of a docker-compose
deployment has no Docker access, so :class:`RemoteBroker` asks the workspace service
(``ai-coding server workspaces``) with short-lived HMAC-signed tickets instead.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import pty
import signal
import struct
import subprocess
import termios
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ai_coding.api.diffs import (
    DIFF_ARGS,
    PATCH_OPS,
    PATH_OPS,
    REVIEW_OPS,
    UNTRACKED_SCRIPT,
    validate_patch,
    validate_path,
)
from ai_coding.api.settings import ServerSettings
from ai_coding.executors.docker import DOCKER_CLIENT_ENV
from ai_coding.executors.egress import PROXY_ENV, start_gateway, stop_gateway
from ai_coding.observability import get_logger

_log = get_logger(__name__)
HOME = "/home/dev"
MAILBOX = "/platform"
WORKSPACE = "/workspace"
PROJECT = "/project"
LABEL = "ai-coding.workspace"
TICKET_SECONDS = 60
GIT_OPS = ("status", "commit", "commit_staged", "pull", "bundle", "diff", *REVIEW_OPS)
_MAX_QUEUE = 256  # output chunks buffered for a slow browser before reading pauses


class BrokerError(Exception):
    """A workspace operation failed; the message is safe to show to the user."""


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """A user's workspace in a project, as paths on the Docker host."""

    project_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    project_root: Path
    repo: Path
    home: Path

    @property
    def container(self) -> str:
        return f"aic-ws-{self.project_id.hex[:12]}-{self.user_id.hex[:12]}"

    @property
    def mailbox(self) -> Path:
        """Where the chat agents' requests to the platform go (ADR-0038)."""
        return self.repo.parent / "mailbox"


@dataclass(slots=True)
class GitOutcome:
    ok: bool
    output: str
    branch: str | None = None
    bundle: bytes | None = None


class TerminalSession(Protocol):
    def output(self) -> AsyncIterator[bytes]: ...
    async def send(self, data: bytes) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...


class Broker(Protocol):
    async def attach(self, ws: WorkspaceRef, cols: int, rows: int) -> TerminalSession: ...
    async def chat(self, ws: WorkspaceRef) -> TerminalSession: ...
    async def git(
        self,
        ws: WorkspaceRef,
        op: str,
        message: str | None = None,
        *,
        patch: str | None = None,
        path: str | None = None,
    ) -> GitOutcome: ...
    async def stop(self, ws: WorkspaceRef) -> None: ...
    async def run(self, stop: asyncio.Event) -> None: ...


def docker_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in DOCKER_CLIENT_ENV}


def _size(value: Any, default: int) -> int:
    try:
        return max(2, min(int(value), 1000))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- local --
class PtySession:
    """``docker exec -it … tmux`` on a pseudo-terminal, read without blocking the loop."""

    def __init__(
        self,
        argv: list[str],
        env: Mapping[str, str],
        cols: int,
        rows: int,
        *,
        on_close: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._on_close = on_close
        self._loop = asyncio.get_running_loop()
        master, slave = pty.openpty()
        _set_size(slave, cols, rows)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv, stdin=slave, stdout=slave, stderr=slave, env=dict(env),
                start_new_session=True, close_fds=True,
            )  # fmt: skip
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._fd = master
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reading = True
        self._closed = False
        self._loop.add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the other side of the terminal is gone
            data = b""
        if not data:
            self._pause()
            self._queue.put_nowait(None)
            return
        self._queue.put_nowait(data)
        if self._queue.qsize() >= _MAX_QUEUE:
            self._pause()

    def _pause(self) -> None:
        if self._reading:
            self._loop.remove_reader(self._fd)
            self._reading = False

    async def output(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            if not self._reading and not self._closed and self._queue.qsize() < _MAX_QUEUE // 4:
                self._reading = True
                self._loop.add_reader(self._fd, self._on_readable)
            yield chunk

    async def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view and not self._closed:
            try:
                view = view[os.write(self._fd, view) :]
            except BlockingIOError:
                await asyncio.sleep(0.01)

    async def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            _set_size(self._fd, cols, rows)  # the kernel signals SIGWINCH to docker exec

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pause()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGHUP)  # tmux detaches; the session stays
        try:
            await asyncio.to_thread(self._proc.wait, 5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGKILL)
            await asyncio.to_thread(self._proc.wait)
        os.close(self._fd)
        if self._on_close is not None:
            # Killing `docker exec` leaves its process in the container running: detach
            # this tab's tmux client there, or the workspace would never look idle.
            with contextlib.suppress(Exception):
                await self._on_close()


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PipeSession:
    """``docker exec -i … ai-coding-agent connect``: the chat daemon's JSON lines."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def output(self) -> AsyncIterator[bytes]:
        stdout = self._proc.stdout
        if stdout is None:
            return
        while True:
            try:
                line = await stdout.readline()
            except ValueError:  # a line over the limit
                continue
            if not line:
                return
            yield line.rstrip(b"\n")

    async def send(self, data: bytes) -> None:
        if self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        self._proc.stdin.write(data.rstrip(b"\n") + b"\n")
        with contextlib.suppress(ConnectionError):
            await self._proc.stdin.drain()

    async def resize(self, cols: int, rows: int) -> None:
        return None

    async def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()


class LocalBroker:
    """Workspace containers on the Docker daemon this process can reach."""

    def __init__(self, settings: ServerSettings, *, docker: str = "docker") -> None:
        self._settings = settings
        self._docker = docker
        self._env = docker_env()
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, float] = {}  # container → last time a terminal was attached
        # Several platform instances may share one Docker daemon (a test server next to
        # production): each one starts, reaps and detaches only its own containers.
        data_dir = str(settings.data_dir.resolve())
        self.instance = hashlib.sha256(data_dir.encode()).hexdigest()[:12]
        self._image_id: str | None = None

    # ---------------------------------------------------------- container --
    def container_argv(self, ws: WorkspaceRef, egress: tuple[str, str] | None) -> list[str]:
        """``docker run`` argv of a workspace container (exposed for review and tests)."""
        s = self._settings
        for path in (ws.repo, ws.home, ws.project_root, ws.mailbox):
            if any(c in str(path) for c in ",\n\r"):
                raise BrokerError(f"unsupported character in the path {path}")
        argv = [
            "run", "-d", "--name", ws.container, "--hostname", "workspace",
            "--pull", "never",  # built by the operator: no hidden egress
            "--label", f"{LABEL}=1", "--label", f"{LABEL}.project={ws.project_id}",
            "--label", f"{LABEL}.user={ws.user_id}",
            "--label", f"{LABEL}.instance={self.instance}",
            "--label", f"{LABEL}.image={self._image_id or s.workspace_image}",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            *(("--runtime", s.workspace_runtime) if s.workspace_runtime else ()),
            "--pids-limit", "1024", "--cpus", str(s.workspace_cpus),
            "--memory", f"{s.workspace_memory_mb}m", "--memory-swap", f"{s.workspace_memory_mb}m",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",  # noqa: S108 - container path
            "--mount", f"type=bind,source={ws.repo},target={WORKSPACE}",
            "--mount", f"type=bind,source={ws.home},target={HOME}",
            "--mount", f"type=bind,source={ws.project_root},target={PROJECT},readonly",
            "--mount", f"type=bind,source={ws.mailbox},target={MAILBOX}",
            "--env", f"AI_CODING_PLATFORM_MAILBOX={MAILBOX}",
            "--env", f"HOME={HOME}", "--env", "TERM=xterm-256color", "--env", "LANG=C.UTF-8",
            "--env", "TMUX_TMPDIR=/tmp", "--env", "DISABLE_AUTOUPDATER=1",
            "--env", f"AI_CODING_PROJECT={ws.project_id}",
            "--workdir", WORKSPACE,
        ]  # fmt: skip
        if egress is not None:
            argv += ["--network", egress[0], "--dns", "0.0.0.0"]  # noqa: S104 - proxy resolves
            for key in PROXY_ENV:
                argv += ["--env", f"{key}={egress[1]}"]
            argv += [
                "--env",
                "NO_PROXY=localhost,127.0.0.1",
                "--env",
                "no_proxy=localhost,127.0.0.1",
            ]
        else:
            argv += ["--network", "bridge"]
        return [*argv, s.workspace_image, "sleep", "infinity"]  # via the image's init entry point

    async def _call(self, *args: str, timeout: float = 120) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            self._docker, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env,
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise BrokerError(f"docker {args[0]} timed out") from None
        return proc.returncode or 0, out, err

    async def _state(self, name: str) -> tuple[str | None, str | None]:
        """The container's status and the image id it was started from."""
        fmt = f'{{{{.State.Status}}}} {{{{index .Config.Labels "{LABEL}.image"}}}}'
        code, out, _ = await self._call("inspect", "--format", fmt, name)
        if code != 0:
            return None, None
        status, _, image = out.decode().strip().partition(" ")
        return status, image or None

    async def _current_image(self) -> str | None:
        code, out, _ = await self._call("image", "inspect", "--format", "{{.Id}}",
                                        self._settings.workspace_image)  # fmt: skip
        self._image_id = out.decode().strip() if code == 0 else None
        return self._image_id

    async def ensure(self, ws: WorkspaceRef) -> None:
        """Start the workspace's container unless it runs. A container started from an
        older image (before an upgrade) is replaced once nobody uses it."""
        lock = self._locks.setdefault(ws.container, asyncio.Lock())
        async with lock:
            state, image = await self._state(ws.container)
            current = await self._current_image()
            if state == "running" and (image == current or await self._in_use(ws.container)):
                return
            if state is not None:
                await self._call("rm", "-f", ws.container)
            await stop_gateway(self._docker, ws.container, self._env)
            ws.mailbox.mkdir(mode=0o700, exist_ok=True)
            egress = None
            if self._settings.workspace_egress != ("*",):
                allow = self._settings.workspace_egress
                try:
                    gateway = await start_gateway(self._docker, ws.container, allow, self._env)
                except Exception as exc:
                    raise BrokerError(f"could not start the network gateway: {exc}") from None
                egress = gateway.route
            code, _, err = await self._call(*self.container_argv(ws, egress))
            if code != 0:
                await stop_gateway(self._docker, ws.container, self._env)
                message = err.decode(errors="replace").strip()
                if "No such image" in message:
                    raise BrokerError(
                        f"the workspace image {self._settings.workspace_image} is not built "
                        "(docs/server.md, «Терминал»)"
                    )
                raise BrokerError(f"could not start the workspace: {message[-500:]}")
            self._active[ws.container] = time.monotonic()
            _log.info("workspace started", container=ws.container)

    async def stop(self, ws: WorkspaceRef) -> None:
        await self._remove(ws.container)

    async def _remove(self, name: str) -> None:
        await self._call("rm", "-f", name)
        await stop_gateway(self._docker, name, self._env)
        self._active.pop(name, None)
        _log.info("workspace stopped", container=name)

    # ---------------------------------------------------------- terminals --
    async def attach(self, ws: WorkspaceRef, cols: int, rows: int) -> TerminalSession:
        await self.ensure(ws)
        self._active[ws.container] = time.monotonic()
        tab = uuid.uuid4().hex  # this tab's tmux client records its pid under this name
        start = 'echo $$ > "/tmp/.aic-tab-$1"; exec tmux -u new-session -A -s main'
        argv = [self._docker, "exec", "-it", "--workdir", WORKSPACE, "--env",
                "TERM=xterm-256color", ws.container, "sh", "-c", start, "sh", tab]  # fmt: skip

        async def detach() -> None:
            script = 'kill -HUP "$(cat "/tmp/.aic-tab-$1")" 2>/dev/null; rm -f "/tmp/.aic-tab-$1"'
            await self._call("exec", ws.container, "sh", "-c", script, "sh", tab, timeout=20)

        return PtySession(argv, self._env, _size(cols, 120), _size(rows, 32), on_close=detach)

    async def chat(self, ws: WorkspaceRef) -> TerminalSession:
        """The chat daemon of the container (started if needed): agents keep running in
        it when the browser goes away."""
        await self.ensure(ws)
        self._active[ws.container] = time.monotonic()
        code, _, _ = await self._call("exec", "-d", ws.container, "ai-coding-agent", "serve",
                                      timeout=30)  # fmt: skip
        if code != 0:
            raise BrokerError(
                "the workspace container predates the chat (an older image): restart the "
                "workspace («Перезапустить среду»); files and sign-ins are kept"
            )
        proc = await asyncio.create_subprocess_exec(
            self._docker, "exec", "-i", ws.container, "ai-coding-agent", "connect",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self._env, limit=16 * 1024 * 1024,
        )  # fmt: skip
        return PipeSession(proc)

    # ---------------------------------------------------------------- git --
    async def _git(self, ws: WorkspaceRef, *args: str) -> tuple[int, bytes, str]:
        code, out, err = await self._call(
            "exec", "--workdir", WORKSPACE, "--env", "GIT_TERMINAL_PROMPT=0", ws.container,
            "git", *args, timeout=300,
        )  # fmt: skip
        message = err.decode(errors="replace")
        if code != 0 and not message.strip():  # docker's own errors come on stdout
            message = out.decode(errors="replace")
        return code, out, message

    async def _git_input(self, ws: WorkspaceRef, data: bytes, *args: str) -> tuple[int, str]:
        """git with ``data`` on its stdin (a patch for ``git apply -``)."""
        proc = await asyncio.create_subprocess_exec(
            self._docker, "exec", "-i", "--workdir", WORKSPACE, "--env", "GIT_TERMINAL_PROMPT=0",
            ws.container, "git", *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env,
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), 120)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise BrokerError("git apply timed out") from None
        return proc.returncode or 0, (out + err).decode(errors="replace").strip()

    async def _diff(self, ws: WorkspaceRef) -> GitOutcome:
        """Accepted (index) and pending (working tree, untracked) changes, as JSON."""
        staged = await self._git(ws, *DIFF_ARGS, "--cached")
        unstaged = await self._git(ws, *DIFF_ARGS)
        for code, _, err in (staged, unstaged):
            if code != 0:
                return GitOutcome(False, err)
        _, untracked, _ = await self._call(
            "exec", "--workdir", WORKSPACE, "--env", "GIT_TERMINAL_PROMPT=0", ws.container,
            "sh", "-c", UNTRACKED_SCRIPT, timeout=300,
        )  # fmt: skip
        texts = [b.decode(errors="replace") for b in (staged[1], unstaged[1], untracked)]
        return GitOutcome(
            True, json.dumps(dict(zip(("staged", "unstaged", "untracked"), texts, strict=True)))
        )

    async def _review(
        self, ws: WorkspaceRef, op: str, patch: str | None, path: str | None
    ) -> GitOutcome:
        try:
            if op in PATCH_OPS:
                code, output = await self._git_input(
                    ws, validate_patch(patch).encode(), *PATCH_OPS[op]
                )
                return GitOutcome(code == 0, output)
            code, out, err = await self._git(ws, *PATH_OPS[op], validate_path(path))
        except ValueError as exc:
            raise BrokerError(str(exc)) from None
        return GitOutcome(code == 0, (out.decode(errors="replace") + err).strip())

    async def git(
        self,
        ws: WorkspaceRef,
        op: str,
        message: str | None = None,
        *,
        patch: str | None = None,
        path: str | None = None,
    ) -> GitOutcome:
        if op not in GIT_OPS:
            raise BrokerError(f"unknown git operation {op!r}")
        await self.ensure(ws)
        if op == "status":
            code, out, err = await self._git(ws, "status", "--porcelain=v1", "--branch")
            return GitOutcome(code == 0, out.decode(errors="replace") if code == 0 else err)
        if op == "diff":
            return await self._diff(ws)
        if op in REVIEW_OPS:
            return await self._review(ws, op, patch, path)
        return await self._branch_op(ws, op, message)

    async def _branch_op(self, ws: WorkspaceRef, op: str, message: str | None) -> GitOutcome:
        """Operations of the current branch: commit, pull, bundle."""
        code, out, _ = await self._git(ws, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = out.decode().strip() if code == 0 else None
        if op in ("commit", "commit_staged"):
            return await self._commit(ws, branch, message, staged_only=op == "commit_staged")
        if branch is None:
            return GitOutcome(False, "the workspace is not on a branch (detached HEAD)")
        if op == "pull":
            return await self._pull(ws, branch)
        code, out, err = await self._git(ws, "bundle", "create", "-", f"refs/heads/{branch}")
        return GitOutcome(code == 0, err.strip(), branch, out if code == 0 else None)

    async def _commit(
        self,
        ws: WorkspaceRef,
        branch: str | None,
        message: str | None,
        *,
        staged_only: bool = False,
    ) -> GitOutcome:
        if not message or not message.strip():
            raise BrokerError("a commit message is required")
        if not staged_only:  # "commit accepted" commits the index alone
            code, _, err = await self._git(ws, "add", "--all")
            if code != 0:
                return GitOutcome(False, err, branch)
        code, out, err = await self._git(ws, "commit", "--quiet", "-m", message)
        return GitOutcome(code == 0, (out.decode(errors="replace") + err).strip(), branch)

    async def _pull(self, ws: WorkspaceRef, branch: str) -> GitOutcome:
        code, _, err = await self._git(ws, "fetch", "--quiet", "project")
        if code != 0:
            return GitOutcome(False, err, branch)
        code, out, err = await self._git(ws, "merge", "--ff-only", f"project/{branch}")
        return GitOutcome(code == 0, (out.decode(errors="replace") + err).strip(), branch)

    # ------------------------------------------------------------- reaping --
    async def run(self, stop: asyncio.Event, *, interval: float = 60.0) -> None:
        """Remove workspace containers without an attached terminal for too long."""
        idle = self._settings.workspace_idle_minutes * 60
        with contextlib.suppress(Exception):
            await self.detach_all()
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
            if stop.is_set():
                return
            try:
                await self.reap(idle)
            except Exception as exc:  # the loop must survive a bad round
                _log.warning("workspace reaper failed", error=str(exc))

    async def _in_use(self, name: str) -> bool:
        """A terminal attached, a chat open or an agent at work in the container."""
        code, clients, _ = await self._call("exec", name, "tmux", "list-clients", timeout=20)
        return (code == 0 and bool(clients.strip())) or await self._agent_active(name)

    async def _agent_active(self, name: str) -> bool:
        """A chat agent at work, or a chat open in a browser, counts as activity."""
        code, out, _ = await self._call("exec", name, "ai-coding-agent", "status", timeout=20)
        try:
            state = json.loads(out) if code == 0 else {}
        except ValueError:
            return False
        return bool(state.get("busy")) or int(state.get("clients") or 0) > 0

    async def _containers(self) -> list[str]:
        code, out, _ = await self._call("ps", "--filter", f"label={LABEL}=1",
                                        "--filter", f"label={LABEL}.instance={self.instance}",
                                        "--format", "{{.Names}}")  # fmt: skip
        return out.decode().split() if code == 0 else []

    async def detach_all(self) -> None:
        """At start-up no browser can be attached yet: tmux clients left by a previous
        server process are stale and would keep their workspaces from looking idle."""
        for name in await self._containers():
            await self._call("exec", name, "tmux", "detach-client", "-s", "main", timeout=20)

    async def reap(self, idle_seconds: float) -> list[str]:
        removed: list[str] = []
        now = time.monotonic()
        for name in await self._containers():
            attached = await self._in_use(name)
            if attached:
                self._active[name] = now
            elif now - self._active.setdefault(name, now) > idle_seconds:
                await self._remove(name)
                removed.append(name)
        return removed


# ------------------------------------------------------------------ tickets --
def sign_ticket(secret: str, project_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """A short-lived proof that the API authorized ``user_id`` in ``project_id``."""
    body = json.dumps({"p": str(project_id), "u": str(user_id),
                       "exp": int(time.time()) + TICKET_SECONDS}).encode()  # fmt: skip
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(mac)}"


def verify_ticket(secret: str, ticket: str) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        body_part, mac_part = ticket.split(".", 1)
        body = _unb64(body_part)
        mac = _unb64(mac_part)
    except (ValueError, TypeError):
        raise BrokerError("invalid ticket") from None
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise BrokerError("invalid ticket")
    data = json.loads(body)
    if int(data["exp"]) < time.time():
        raise BrokerError("expired ticket")
    return uuid.UUID(data["p"]), uuid.UUID(data["u"])


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ------------------------------------------------------------------ remote --
class RemoteSession:
    def __init__(self, connection: Any) -> None:
        self._ws = connection

    async def output(self) -> AsyncIterator[bytes]:
        async for message in self._ws:
            if isinstance(message, bytes):
                yield message
                continue
            control = json.loads(message)
            if control.get("type") == "error":
                raise BrokerError(str(control.get("message", "workspace error")))
            if control.get("type") == "exit":
                return

    async def send(self, data: bytes) -> None:
        await self._ws.send(json.dumps({"type": "input", "data": data.decode(errors="replace")}))

    async def resize(self, cols: int, rows: int) -> None:
        await self._ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

    async def close(self) -> None:
        await self._ws.close()


class RemoteBroker:
    """The workspace service of a deployment whose API has no Docker access."""

    def __init__(self, settings: ServerSettings) -> None:
        self._url = settings.workspaces
        self._secret = settings.workspace_secret

    def _ticket(self, ws: WorkspaceRef) -> str:
        if not self._secret:
            raise BrokerError("AI_CODING_SERVER_WORKSPACE_SECRET is not set")
        return sign_ticket(self._secret, ws.project_id, ws.user_id)

    async def attach(self, ws: WorkspaceRef, cols: int, rows: int) -> TerminalSession:
        import websockets  # noqa: PLC0415 - server extra

        url = "ws" + self._url.removeprefix("http") + "/terminal"
        try:
            connection = await websockets.connect(url, max_size=2**20, open_timeout=30)
            await connection.send(json.dumps({"ticket": self._ticket(ws), "cols": cols,
                                              "rows": rows}))  # fmt: skip
        except OSError as exc:
            raise BrokerError(f"the workspace service is unreachable: {exc}") from None
        return RemoteSession(connection)

    async def chat(self, ws: WorkspaceRef) -> TerminalSession:
        import websockets  # noqa: PLC0415 - server extra

        url = "ws" + self._url.removeprefix("http") + "/chat"
        try:
            connection = await websockets.connect(url, max_size=16 * 2**20, open_timeout=60)
            await connection.send(json.dumps({"ticket": self._ticket(ws)}))
        except OSError as exc:
            raise BrokerError(f"the workspace service is unreachable: {exc}") from None
        return RemoteSession(connection)

    async def git(
        self,
        ws: WorkspaceRef,
        op: str,
        message: str | None = None,
        *,
        patch: str | None = None,
        path: str | None = None,
    ) -> GitOutcome:
        import httpx  # noqa: PLC0415

        payload = {"ticket": self._ticket(ws), "op": op, "message": message, "patch": patch,
                   "path": path}  # fmt: skip
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                response = await client.post(f"{self._url}/git", json=payload)
        except httpx.HTTPError as exc:
            raise BrokerError(f"the workspace service is unreachable: {exc}") from None
        if response.status_code >= 400:
            raise BrokerError(response.json().get("message", "workspace error"))
        if response.headers.get("content-type", "").startswith("application/octet-stream"):
            return GitOutcome(True, "", response.headers.get("x-branch"), response.content)
        data = response.json()
        return GitOutcome(bool(data["ok"]), str(data["output"]), data.get("branch"))

    async def stop(self, ws: WorkspaceRef) -> None:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=60) as client:
            await client.post(f"{self._url}/stop", json={"ticket": self._ticket(ws)})

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()  # the service reaps its own containers


def make_broker(settings: ServerSettings) -> Broker | None:
    if settings.workspaces == "off":
        return None
    if settings.workspaces == "local":
        return LocalBroker(settings)
    return RemoteBroker(settings)
