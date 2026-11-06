"""Workspaces (ADR-0036): file access confined to the workspace, roles, the workspace
flow through the API, tickets, the docker argv, and the browser terminal's WebSocket."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import tarfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_coding.api import files
from ai_coding.api.app import create_app
from ai_coding.api.core import ConflictError, InvalidError, NotFoundError
from ai_coding.api.diffs import (
    DIFF_ARGS,
    PATCH_OPS,
    PATH_OPS,
    UNTRACKED_SCRIPT,
    review_payload,
    validate_patch,
    validate_path,
)
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.terminal import create_workspace_app, parse_status
from ai_coding.api.workspace_broker import (
    BrokerError,
    GitOutcome,
    LocalBroker,
    WorkspaceRef,
    sign_ticket,
    verify_ticket,
)
from ai_coding.domain.enums import Role

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


# ------------------------------------------------------------------- files --
def test_files_stay_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("other project's secret", encoding="utf-8")
    (root / "leak.txt").symlink_to(secret)
    (root / "out").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / "pipe")

    version = files.write_file(root, "src/app.py", "print(1)\n")
    assert files.read_file(root, "src/app.py")["content"] == "print(1)\n"
    names = {e["name"]: e["type"] for e in files.list_dir(root, "")}
    assert names == {"src": "dir", "leak.txt": "link", "out": "link", "pipe": "other"}
    for path in ("leak.txt", "out/secret.txt", "../secret.txt", ".git/config", "a\0b"):
        with pytest.raises(InvalidError):
            files.read_file(root, path)
    with pytest.raises(InvalidError):  # never written through a link either
        files.write_file(root, "out/new.txt", "x")
    assert not (tmp_path / "new.txt").exists()
    with pytest.raises(InvalidError):  # a FIFO would block a request: refused at once
        files.read_file(root, "pipe")
    with pytest.raises(NotFoundError):
        files.read_file(root, "missing.py")

    # Saves are checked against the version loaded in the editor.
    with pytest.raises(ConflictError):
        files.write_file(root, "src/app.py", "stale", expected="0" * 32)
    assert files.write_file(root, "src/app.py", "print(2)\n", expected=version) != version
    with pytest.raises(ConflictError):
        files.write_file(root, "src/app.py", "", create=True)
    (root / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "run.sh").chmod(0o755)
    files.write_file(root, "run.sh", "#!/bin/sh\necho hi\n")
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o755  # permission bits kept
    (root / "data.bin").write_bytes(b"\x00\x01binary")
    assert files.read_file(root, "data.bin")["binary"] is True
    files.delete_file(root, "leak.txt")  # the link itself, not its target
    assert secret.exists() and not (root / "leak.txt").exists()
    with pytest.raises(InvalidError):
        files.delete_file(root, "src")


# ------------------------------------------------------------------ broker --
def _settings(tmp_path: Path, **kw: object) -> ServerSettings:
    roots = tmp_path / "repos"
    roots.mkdir(exist_ok=True)
    base = ServerSettings(data_dir=tmp_path / "data",
                          database_url=f"sqlite:///{tmp_path / 's.db'}",
                          allowed_roots=(roots.resolve(),), workspace_secret="s3cret")  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


def _ref(tmp_path: Path) -> WorkspaceRef:
    return WorkspaceRef(uuid.uuid4(), uuid.uuid4(), "dev", tmp_path / "project",
                        tmp_path / "ws" / "repo", tmp_path / "home")  # fmt: skip


def test_container_is_locked_down(tmp_path: Path) -> None:
    broker = LocalBroker(_settings(tmp_path, workspace_runtime="runsc"))
    ref = _ref(tmp_path)
    argv = broker.container_argv(ref, ("aic-net", "http://10.0.0.2:3128"))
    joined = " ".join(argv)
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--runtime runsc",
                 "--pull never", "--network aic-net", "--dns 0.0.0.0",
                 "HTTPS_PROXY=http://10.0.0.2:3128"):  # fmt: skip
        assert flag in joined, flag
    assert f"source={ref.project_root},target=/project,readonly" in joined
    assert f"source={ref.repo},target=/workspace" in joined
    assert "/var/run/docker.sock" not in joined
    # Instances sharing a Docker daemon (a test server next to production) keep apart.
    assert f"ai-coding.workspace.instance={broker.instance}" in joined
    (tmp_path / "other").mkdir()
    other = LocalBroker(_settings(tmp_path / "other"))
    assert other.instance != broker.instance
    assert argv[-3:] == ["ai-coding-workspace:local", "sleep", "infinity"]
    assert "--network bridge" in " ".join(broker.container_argv(ref, None))  # egress "*"
    with pytest.raises(BrokerError):
        broker.container_argv(replace(ref, home=tmp_path / "a,b"), None)


def test_tickets() -> None:
    project, user = uuid.uuid4(), uuid.uuid4()
    ticket = sign_ticket("secret", project, user)
    assert verify_ticket("secret", ticket) == (project, user)
    for bad in (sign_ticket("other", project, user), ticket[:-2] + "xx", "garbage", ""):
        with pytest.raises(BrokerError):
            verify_ticket("secret", bad)


def test_parse_status() -> None:
    out = "## main...origin/main [ahead 1]\n M src/app.py\n?? new.txt\n"
    assert parse_status(out) == {"branch": "main", "files": [
        {"status": " M", "path": "src/app.py"}, {"status": "??", "path": "new.txt"}]}  # fmt: skip
    assert parse_status("## No commits yet on main\n")["branch"] == "main"
    assert parse_status("## HEAD (no branch)\n")["branch"] is None


# --------------------------------------------------------------------- API --
class FakeSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sizes: list[tuple[int, int]] = []
        self.closed = False

    async def output(self) -> AsyncIterator[bytes]:
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def send(self, data: bytes) -> None:
        await self.queue.put(b"echo:" + data)

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def close(self) -> None:
        self.closed = True


class FakeChat(FakeSession):
    """Plays the chat daemon: echoes each request as an event."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict[str, object]] = []

    async def send(self, data: bytes) -> None:
        request = json.loads(data)
        self.received.append(request)
        await self.queue.put(json.dumps({"t": "echo", "op": request["op"]}).encode())


class FakeBroker:
    """Plays the user's container: git runs on the workspace as the container would."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.attached: list[WorkspaceRef] = []
        self.chats: list[FakeChat] = []
        self.stopped: list[WorkspaceRef] = []

    async def chat(self, ws: WorkspaceRef) -> FakeChat:
        self.attached.append(ws)
        chat = FakeChat()
        self.chats.append(chat)
        return chat

    async def attach(self, ws: WorkspaceRef, cols: int, rows: int) -> FakeSession:
        self.attached.append(ws)
        session = FakeSession()
        session.sizes.append((cols, rows))
        self.sessions.append(session)
        return session

    async def git(
        self,
        ws: WorkspaceRef,
        op: str,
        message: str | None = None,
        *,
        patch: str | None = None,
        path: str | None = None,
    ) -> GitOutcome:
        """The broker's git, on the host: the same commands as in the container."""

        def git(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(["git", *args], cwd=ws.repo, input=data, capture_output=True,
                                  check=False)  # fmt: skip

        branch = git("symbolic-ref", "--short", "HEAD").stdout.decode().strip()
        if op == "status":
            return GitOutcome(True, git("status", "--porcelain=v1", "--branch").stdout.decode())
        if op == "diff":
            untracked = subprocess.run(["sh", "-c", UNTRACKED_SCRIPT], cwd=ws.repo,
                                       capture_output=True, check=False).stdout  # fmt: skip
            texts = {"staged": git(*DIFF_ARGS, "--cached").stdout.decode(),
                     "unstaged": git(*DIFF_ARGS).stdout.decode(),
                     "untracked": untracked.decode()}  # fmt: skip
            return GitOutcome(True, json.dumps(texts))
        if op in PATCH_OPS or op in PATH_OPS:
            done = (git(*PATCH_OPS[op], data=validate_patch(patch).encode()) if op in PATCH_OPS
                    else git(*PATH_OPS[op], validate_path(path)))  # fmt: skip
            return GitOutcome(done.returncode == 0, (done.stdout + done.stderr).decode())
        if op in ("commit", "commit_staged"):
            if op == "commit":
                git("add", "--all")
            done = git("commit", "-q", "-m", message or "")
            return GitOutcome(done.returncode == 0, done.stdout.decode(), branch)
        if op == "bundle":
            done = git("bundle", "create", "-", f"refs/heads/{branch}")
            return GitOutcome(True, "", branch, done.stdout)
        return GitOutcome(True, "Already up to date.", branch)

    async def stop(self, ws: WorkspaceRef) -> None:
        self.stopped.append(ws)

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()


def _tar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def world(tmp_path: Path, git_env: None) -> Iterator[dict[str, object]]:
    srv = Server(_settings(tmp_path))
    srv.migrate()
    for name in ("dev", "auditor", "outsider", "other"):
        srv.create_user(name, PASSWORD)
    broker = FakeBroker()
    with TestClient(create_app(srv, broker=broker)) as client:  # type: ignore[arg-type]
        heads = {}
        for name in ("dev", "auditor", "outsider", "other"):
            creds = {"username": name, "password": PASSWORD}
            token = client.post("/api/v1/auth/login", json=creds).json()["token"]
            heads[name] = {"Authorization": f"Bearer {token}"}
        archive = _tar({"README.md": b"# shop\n", "src/app.py": b"x = 1\n"})
        made = client.post("/api/v1/projects/upload", params={"name": "shop"},
                           headers={**heads["dev"], "Content-Type": "application/x-tar"},
                           content=archive)  # fmt: skip
        assert made.status_code == 201, made.text
        pid = made.json()["id"]
        dev = srv.login("dev", PASSWORD)
        assert dev is not None
        srv.set_member(dev, uuid.UUID(pid), "auditor", Role.AUDITOR)
        srv.set_member(dev, uuid.UUID(pid), "other", Role.DEVELOPER)
        yield {"client": client, "server": srv, "pid": pid, "h": heads, "broker": broker,
               "tmp": tmp_path}  # fmt: skip
    srv.close()


def test_workspace_flow(world: dict[str, object]) -> None:
    c, srv, pid, h = world["client"], world["server"], world["pid"], world["h"]
    assert isinstance(c, TestClient) and isinstance(srv, Server) and isinstance(h, dict)
    base = f"/api/v1/projects/{pid}"
    info = c.get(f"{base}/workspace", headers=h["dev"]).json()
    assert info == {"git": True, "exists": False, "can_edit": True, "terminal": "on",
                    "branch_prefix": "ws/dev/"}  # fmt: skip
    # Before the workspace exists, the project's files can be read, never written.
    listed = c.get(f"{base}/files", params={"scope": "project"}, headers=h["dev"]).json()
    assert {e["name"] for e in listed} >= {"README.md", "src"}
    assert c.get(f"{base}/files", headers=h["dev"]).status_code == 409  # no workspace yet
    assert c.put(f"{base}/file", params={"path": "x"}, json={"content": "x"},
                 headers=h["dev"]).status_code == 409  # fmt: skip

    opened = c.post(f"{base}/workspace", headers=h["dev"])
    assert opened.status_code == 201 and opened.json()["exists"] is True
    dev = srv.login("dev", PASSWORD)
    assert dev is not None
    repo = srv.settings.workspaces_dir / pid / str(dev.user_id) / "repo"
    remotes = subprocess.run(["git", "remote", "-v"], cwd=repo, capture_output=True, text=True,
                             check=True).stdout  # fmt: skip
    assert "project\t/project" in remotes and "origin" not in remotes  # uploaded: no origin
    home = srv.settings.homes_dir / str(dev.user_id)
    assert "dev@users.ai-coding.invalid" in (home / ".gitconfig").read_text(encoding="utf-8")

    read = c.get(f"{base}/file", params={"path": "src/app.py"}, headers=h["dev"]).json()
    assert read["content"] == "x = 1\n"
    app_py = {"path": "src/app.py"}
    saved = c.put(f"{base}/file", params=app_py, headers=h["dev"],
                  json={"content": "x = 2\n", "version": read["version"]})  # fmt: skip
    assert saved.status_code == 200
    stale = c.put(f"{base}/file", params=app_py, headers=h["dev"],
                  json={"content": "x = 3\n", "version": read["version"]})  # fmt: skip
    assert stale.status_code == 409
    created = c.put(f"{base}/file", params={"path": "docs/notes.md"},
                    json={"content": "", "create": True}, headers=h["dev"])  # fmt: skip
    assert created.status_code == 200
    # A link planted in the workspace (the user has a shell) leads nowhere.
    (repo / "escape").symlink_to(srv.settings.data_dir, target_is_directory=True)
    assert c.get(f"{base}/files", params={"path": "escape"}, headers=h["dev"]).status_code == 422

    status = c.get(f"{base}/workspace/git", headers=h["dev"]).json()
    assert status["branch"] == "main"
    assert {"status": " M", "path": "src/app.py"} in status["files"]
    committed = c.post(f"{base}/workspace/commit", json={"message": "x = 2"}, headers=h["dev"])
    assert committed.json()["ok"] is True
    published = c.post(f"{base}/workspace/publish", headers=h["dev"]).json()
    assert published["branch"] == "ws/dev/main"
    project_root = srv.settings.projects_dir / pid
    shown = subprocess.run(["git", "show", "ws/dev/main:src/app.py"], cwd=project_root,
                           capture_output=True, text=True, check=True).stdout  # fmt: skip
    assert shown == "x = 2\n"
    main = subprocess.run(["git", "show", "main:src/app.py"], cwd=project_root,
                          capture_output=True, text=True, check=True).stdout  # fmt: skip
    assert main == "x = 1\n"  # the project's own branch is untouched
    assert c.delete(f"{base}/file", params={"path": "docs/notes.md"},
                    headers=h["dev"]).status_code == 204  # fmt: skip
    actions = {e["action"] for e in c.get("/api/v1/audit-events", params={"project_id": pid},
                                          headers=h["dev"]).json()}  # fmt: skip
    assert {"workspace.create", "workspace.file.write", "workspace.publish"} <= actions


def test_changes_review(world: dict[str, object]) -> None:
    """AI Changes Review (ADR-0040): the workspace's changes by file and hunk; accept stages
    a hunk, reject reverts it, a new file is added or removed as a whole, "commit accepted"
    commits the index alone; stale hunks are refused; decisions are audited."""
    c, pid, h = world["client"], world["pid"], world["h"]
    assert isinstance(c, TestClient) and isinstance(h, dict)
    base = f"/api/v1/projects/{pid}"
    c.post(f"{base}/workspace", headers=h["dev"])
    lines = [f"line {i}\n" for i in range(1, 21)]
    put = lambda path, text, **kw: c.put(f"{base}/file", params={"path": path}, headers=h["dev"],  # noqa: E731
                                         json={"content": text, "create": True, **kw})  # fmt: skip
    assert put("src/long.py", "".join(lines)).status_code == 200
    assert c.post(f"{base}/workspace/commit", json={"message": "long"}, headers=h["dev"]).json()[
        "ok"
    ]
    lines[1], lines[17] = "LINE 2\n", "LINE 18\n"
    version = c.get(f"{base}/file", params={"path": "src/long.py"}, headers=h["dev"]).json()[
        "version"
    ]
    assert put("src/long.py", "".join(lines), version=version, create=False).status_code == 200
    put("docs/keep.md", "# keep\n")
    put("docs/drop.md", "# drop\n")

    diff = c.get(f"{base}/workspace/diff", headers=h["dev"]).json()
    assert diff["accepted"] == [] and not diff["truncated"]
    (long,) = diff["pending"]
    assert long["path"] == "src/long.py" and len(long["hunks"]) == 2
    assert sorted(f["path"] for f in diff["new_files"]) == ["docs/drop.md", "docs/keep.md"]
    first, second = long["hunks"]

    review = lambda **body: c.post(f"{base}/workspace/review", json=body, headers=h["dev"])  # noqa: E731
    assert review(action="accept", patch=first["patch"]).status_code == 200
    assert review(action="reject", patch=second["patch"]).status_code == 200
    assert review(action="reject", patch=second["patch"]).status_code == 409  # gone already
    assert review(action="accept_file", path="docs/keep.md").status_code == 200
    assert review(action="reject_file", path="docs/drop.md").status_code == 200
    assert review(action="reject_file", path="src/long.py").json() == {
        "ok": True,
        "output": "",
    }  # tracked: kept
    assert review(action="accept", patch="rm -rf /").status_code == 422
    assert review(action="accept_file", path="../escape").status_code == 422

    diff = c.get(f"{base}/workspace/diff", headers=h["dev"]).json()
    assert sorted(f["path"] for f in diff["accepted"]) == ["docs/keep.md", "src/long.py"]
    assert diff["pending"] == [] and diff["new_files"] == []
    text = c.get(f"{base}/file", params={"path": "src/long.py"}, headers=h["dev"]).json()["content"]
    assert "LINE 2\n" in text and "line 18\n" in text and "LINE 18" not in text
    assert (
        c.get(f"{base}/file", params={"path": "docs/drop.md"}, headers=h["dev"]).status_code == 404
    )

    # Commit accepted: the index alone; a change made later stays for review.
    put("docs/later.md", "# later\n")
    done = c.post(f"{base}/workspace/commit", json={"message": "reviewed", "staged_only": True},
                  headers=h["dev"])  # fmt: skip
    assert done.json()["ok"] is True
    diff = c.get(f"{base}/workspace/diff", headers=h["dev"]).json()
    assert diff["accepted"] == [] and [f["path"] for f in diff["new_files"]] == ["docs/later.md"]

    assert c.get(f"{base}/workspace/diff", headers=h["auditor"]).status_code == 403
    assert review(action="accept_file", path="docs/later.md").status_code == 200
    events = c.get("/api/v1/audit-events", params={"project_id": pid}, headers=h["dev"]).json()
    reviews = [e for e in events if e["action"] == "workspace.review"]
    assert {(e["target"], e["details"]["path"]) for e in reviews} >= {
        ("accept", "src/long.py"),
        ("reject", "src/long.py"),
        ("reject_file", "docs/drop.md"),
    }


def test_workspace_roles_and_isolation(world: dict[str, object]) -> None:
    c, pid, h = world["client"], world["pid"], world["h"]
    assert isinstance(c, TestClient) and isinstance(h, dict)
    base = f"/api/v1/projects/{pid}"
    # An auditor reads the project, has no workspace.
    info = c.get(f"{base}/workspace", headers=h["auditor"]).json()
    assert info["can_edit"] is False
    assert c.get(f"{base}/file", params={"path": "README.md", "scope": "project"},
                 headers=h["auditor"]).json()["content"] == "# shop\n"  # fmt: skip
    assert c.post(f"{base}/workspace", headers=h["auditor"]).status_code == 403
    # A non-member sees nothing, as if the project did not exist.
    for method, path in (("GET", "/workspace"), ("GET", "/files?scope=project"),
                         ("POST", "/workspace"), ("GET", "/workspace/git")):  # fmt: skip
        assert c.request(method, base + path, headers=h["outsider"]).status_code == 404
    # Two developers get two separate workspaces.
    c.post(f"{base}/workspace", headers=h["dev"])
    c.post(f"{base}/workspace", headers=h["other"])
    c.put(f"{base}/file", params={"path": "mine.txt"}, json={"content": "dev"}, headers=h["dev"])
    theirs = c.get(f"{base}/files", headers=h["other"]).json()
    assert "mine.txt" not in {e["name"] for e in theirs}
    # Saves always go to the caller's workspace, never into the project itself.
    saved = c.put(f"{base}/file", params={"path": "x", "scope": "project"},
                  json={"content": "x"}, headers=h["dev"])  # fmt: skip
    assert saved.status_code == 200
    assert not (Path(str(world["tmp"])) / "data" / "projects" / pid / "x").exists()


def test_terminal_websocket(world: dict[str, object]) -> None:
    c, pid, h, broker = world["client"], world["pid"], world["h"], world["broker"]
    assert isinstance(c, TestClient) and isinstance(h, dict) and isinstance(broker, FakeBroker)
    url = f"/api/v1/projects/{pid}/workspace/terminal"
    token = h["dev"]["Authorization"].removeprefix("Bearer ")
    with c.websocket_connect(url) as ws:  # before the workspace exists
        ws.send_text(json.dumps({"token": token}))
        assert "open the workspace" in json.loads(ws.receive_text())["message"]
    c.post(f"/api/v1/projects/{pid}/workspace", headers=h["dev"])
    with c.websocket_connect(url) as ws:
        ws.send_text(json.dumps({"token": token, "cols": 100, "rows": 40}))
        ws.send_text(json.dumps({"type": "input", "data": "ls\r"}))
        assert ws.receive_bytes() == b"echo:ls\r"
        ws.send_text(json.dumps({"type": "resize", "cols": 80, "rows": 20}))
        ws.send_text("not json")  # ignored
        ws.send_text(json.dumps({"type": "input", "data": "pwd\r"}))
        assert ws.receive_bytes() == b"echo:pwd\r"
    session = broker.sessions[-1]
    assert session.sizes == [(100, 40), (80, 20)]
    for first in ({"token": "aic_" + "x" * 43}, {"nothing": 1}):
        with c.websocket_connect(url) as ws:
            ws.send_text(json.dumps(first))
            assert json.loads(ws.receive_text())["type"] == "error"
    outsider = h["outsider"]["Authorization"].removeprefix("Bearer ")
    with c.websocket_connect(url) as ws:
        ws.send_text(json.dumps({"token": outsider}))
        assert json.loads(ws.receive_text())["message"] == "project not found"
    assert len(broker.attached) == 1


def test_chat_websocket(world: dict[str, object]) -> None:
    """The browser's chat reaches the daemon of the caller's container; only the chat's
    operations go through."""
    c, pid, h, broker = world["client"], world["pid"], world["h"], world["broker"]
    assert isinstance(c, TestClient) and isinstance(h, dict) and isinstance(broker, FakeBroker)
    c.post(f"/api/v1/projects/{pid}/workspace", headers=h["dev"])
    url = f"/api/v1/projects/{pid}/workspace/chat"
    token = h["dev"]["Authorization"].removeprefix("Bearer ")
    with c.websocket_connect(url) as ws:
        ws.send_text(json.dumps({"token": token}))
        ws.send_text(json.dumps({"op": "hello"}))
        assert json.loads(ws.receive_text()) == {"t": "echo", "op": "hello"}
        ws.send_text(json.dumps({"op": "status"}))  # internal to the container
        ws.send_text("not json")
        ws.send_text(json.dumps(["op", "send"]))
        ws.send_text(json.dumps({"op": "send", "session": "s1", "text": "привет"}))
        assert json.loads(ws.receive_text()) == {"t": "echo", "op": "send"}
    (chat,) = broker.chats
    assert [r["op"] for r in chat.received] == ["hello", "send"]
    assert chat.received[1]["text"] == "привет"
    auditor = h["auditor"]["Authorization"].removeprefix("Bearer ")
    with c.websocket_connect(url) as ws:
        ws.send_text(json.dumps({"token": auditor}))
        assert json.loads(ws.receive_text())["type"] == "error"
    assert len(broker.chats) == 1
    actions = {e["action"] for e in c.get("/api/v1/audit-events", params={"project_id": pid},
                                          headers=h["dev"]).json()}  # fmt: skip
    assert "workspace.chat.open" in actions


def test_restart_workspace(world: dict[str, object]) -> None:
    """«Перезапустить среду»: the caller's container only; a new one starts on next use."""
    c, pid, h, broker = world["client"], world["pid"], world["h"], world["broker"]
    assert isinstance(c, TestClient) and isinstance(h, dict) and isinstance(broker, FakeBroker)
    url = f"/api/v1/projects/{pid}/workspace/restart"
    assert c.post(url, headers=h["dev"]).status_code == 409  # no workspace yet
    c.post(f"/api/v1/projects/{pid}/workspace", headers=h["dev"])
    assert c.post(url, headers=h["dev"]).json() == {"status": "restarted"}
    (stopped,) = broker.stopped
    assert stopped.username == "dev"
    assert c.post(url, headers=h["outsider"]).status_code == 404
    assert c.post(url, headers=h["auditor"]).status_code == 403


def test_workspace_service(world: dict[str, object]) -> None:
    """The service of a docker-compose deployment trusts a ticket for who is asking only:
    the role is checked again."""
    c, srv, pid, h = world["client"], world["server"], world["pid"], world["h"]
    assert isinstance(c, TestClient) and isinstance(srv, Server) and isinstance(h, dict)
    c.post(f"/api/v1/projects/{pid}/workspace", headers=h["dev"])
    broker = FakeBroker()
    dev = srv.login("dev", PASSWORD)
    auditor = srv.login("auditor", PASSWORD)
    assert dev is not None and auditor is not None
    with TestClient(create_workspace_app(srv, broker)) as service:  # type: ignore[arg-type]
        assert service.get("/healthz").json() == {"status": "ok"}
        ticket = sign_ticket("s3cret", uuid.UUID(pid), dev.user_id)
        status = service.post("/git", json={"ticket": ticket, "op": "status"})
        assert status.status_code == 200 and status.json()["ok"] is True
        bundle = service.post("/git", json={"ticket": ticket, "op": "bundle"})
        assert bundle.headers["content-type"] == "application/octet-stream"
        assert bundle.headers["x-branch"] == "main" and bundle.content.startswith(b"# v")
        forged = sign_ticket("wrong", uuid.UUID(pid), dev.user_id)
        assert service.post("/git", json={"ticket": forged, "op": "status"}).status_code == 400
        not_allowed = sign_ticket("s3cret", uuid.UUID(pid), auditor.user_id)
        assert service.post("/git", json={"ticket": not_allowed, "op": "status"}).status_code \
            == 403  # fmt: skip
        with service.websocket_connect("/terminal") as ws:
            ws.send_text(json.dumps({"ticket": ticket}))
            ws.send_text(json.dumps({"type": "input", "data": "hi"}))
            assert ws.receive_bytes() == b"echo:hi"
    with pytest.raises(ValueError, match="WORKSPACE_SECRET"):
        create_workspace_app(type("S", (), {"settings": replace(srv.settings,
                                                                workspace_secret=None)})(),
                             broker)  # type: ignore[arg-type]  # fmt: skip


def test_workspaces_need_git_and_can_be_off(tmp_path: Path, git_env: None) -> None:
    srv = Server(_settings(tmp_path, workspaces="off"))
    srv.migrate()
    srv.create_user("dev", PASSWORD)
    plain = srv.settings.allowed_roots[0] / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("1\n", encoding="utf-8")
    try:
        with TestClient(create_app(srv)) as c:
            creds = {"username": "dev", "password": PASSWORD}
            token = c.post("/api/v1/auth/login", json=creds).json()["token"]
            h = {"Authorization": f"Bearer {token}"}
            pid = c.post("/api/v1/projects", headers=h,
                         json={"name": "plain", "path": str(plain)}).json()["id"]  # fmt: skip
            info = c.get(f"/api/v1/projects/{pid}/workspace", headers=h).json()
            assert info["git"] is False and info["terminal"] == "off"
            assert c.post(f"/api/v1/projects/{pid}/workspace", headers=h).status_code == 422
    finally:
        srv.close()


# ------------------------------------------------------------------ docker --
async def review_in_container(broker: LocalBroker, ref: WorkspaceRef, repo: Path) -> None:
    """The review's git in the container: the diff, a hunk rejected through stdin, a new
    file accepted."""
    (repo / "a.py").write_text("x = 99\n", encoding="utf-8")
    outcome = await broker.git(ref, "diff")
    assert outcome.ok, outcome.output
    texts = json.loads(outcome.output)
    payload = review_payload(texts["staged"], texts["unstaged"], texts["untracked"])
    (changed,) = payload["pending"]
    assert changed["path"] == "a.py" and [f["path"] for f in payload["new_files"]] == ["b.py"]
    rejected = await broker.git(ref, "reject", patch=changed["hunks"][0]["patch"])
    assert rejected.ok, rejected.output
    accepted = await broker.git(ref, "accept_file", path="b.py")
    assert accepted.ok, accepted.output


def test_real_workspace_container(tmp_path: Path, git_env: None) -> None:
    """With Docker and the workspace image: a terminal runs in the user's container, git
    runs there too, and the idle container is reaped."""
    from tests.integration.conftest import require_images  # noqa: PLC0415

    require_images("ai-coding-workspace:local")
    settings = _settings(tmp_path, workspace_egress=("*",))
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True)
    repo = tmp_path / "ws" / "repo"
    subprocess.run(["git", "clone", "-q", str(project), str(repo)], check=True)
    subprocess.run(["git", "remote", "set-url", "origin", "/project"], cwd=repo, check=True)
    home = tmp_path / "user-home"  # tmp_path/home is git_env's HOME
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = dev\n\temail = dev@example.invalid\n"
                                     "[safe]\n\tdirectory = *\n", encoding="utf-8")  # fmt: skip
    ref = WorkspaceRef(uuid.uuid4(), uuid.uuid4(), "dev", project, repo, home)
    broker = LocalBroker(settings)

    async def scenario() -> tuple[bytes, GitOutcome, list[str]]:
        session = await broker.attach(ref, 100, 30)
        await session.send(b"echo READY-$((40+2)); claude --version; codex --version\r")
        seen = b""
        deadline = time.monotonic() + 60
        async for chunk in session.output():
            seen += chunk
            if (b"codex-cli" in seen and b"READY-42" in seen) or time.monotonic() > deadline:
                break
        await session.close()
        (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
        await review_in_container(broker, ref, repo)
        status = await broker.git(ref, "status")
        removed = await broker.reap(idle_seconds=0)
        return seen, status, removed

    try:
        output, status, removed = asyncio.run(scenario())
    finally:
        asyncio.run(broker.stop(ref))
    assert b"READY-42" in output
    assert b"2.1.309 (Claude Code)" in output and b"codex-cli 0.163.0" in output
    assert "A  b.py" in status.output  # accepted in the review: staged
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\n"  # rejected: reverted
    assert ref.container in removed
