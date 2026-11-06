"""The orchestrator as tools of the chat agents (ADR-0038): the API serves the mailbox of
each workspace — identity from where a request is, the user's role in that project only,
the workspace examined as a snapshot of its committed branch, the mailbox's files treated
as untrusted."""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from ai_coding.api import agent_bridge
from ai_coding.api.agent_bridge import AgentBridge
from ai_coding.api.services import Principal, Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import Approval, AuditEvent, Run, Task

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


@pytest.fixture
def server(tmp_path: Path, git_env: None) -> Iterator[Server]:
    roots = tmp_path / "repos"
    for name in ("alpha", "beta"):
        repo = roots / name
        repo.mkdir(parents=True)
        (repo / "main.py").write_text("print(1)\n", encoding="utf-8")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
    srv = Server(ServerSettings(data_dir=tmp_path / "data",
                                database_url=f"sqlite:///{tmp_path / 'server.db'}",
                                allowed_roots=(roots.resolve(),)))  # fmt: skip
    srv.migrate()
    yield srv
    srv.close()


def login(server: Server, name: str, *, admin: bool = False) -> Principal:
    server.create_user(name, PASSWORD, is_admin=admin)
    principal = server.login(name, PASSWORD)
    assert principal is not None
    return principal


class Box:
    """The mailbox of a user's workspace, as the container sees it (``/platform``)."""

    def __init__(self, server: Server, principal: Principal, project_id: uuid.UUID) -> None:
        server.create_workspace(principal, project_id)
        self.repo = (
            server.settings.workspaces_dir / str(project_id) / str(principal.user_id) / "repo"
        )
        self.path = self.repo.parent / "mailbox"
        self.path.mkdir()
        self.bridge = AgentBridge(server)

    def ask(self, op: str, *, bundle: str | None = None, **payload: Any) -> dict[str, Any]:
        rid = uuid.uuid4().hex
        if bundle is not None:
            git(self.repo, "bundle", "create", str(self.path / f"{rid}.bundle"), bundle)
        (self.path / f"{rid}.req").write_text(json.dumps({"op": op, **payload}), encoding="utf-8")
        assert self.bridge.poll() == 1
        answer = self.path / f"{rid}.res"
        data = json.loads(answer.read_text(encoding="utf-8"))
        answer.unlink()
        assert sorted(p.name for p in self.path.iterdir()) == []  # nothing left behind
        return dict(data)

    def commit(self, branch: str, files: dict[str, str]) -> str:
        git(self.repo, "checkout", "-q", "-B", branch)
        for name, text in files.items():
            (self.repo / name).write_text(text, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "work")
        return git(self.repo, "rev-parse", "HEAD")


@pytest.fixture
def owner(server: Server) -> Principal:
    return login(server, "owner", admin=True)


@pytest.fixture
def alpha(server: Server, owner: Principal) -> uuid.UUID:
    return server.create_project(
        owner, "alpha", path=str(server.settings.allowed_roots[0] / "alpha")
    )


@pytest.fixture
def dev(server: Server, owner: Principal, alpha: uuid.UUID) -> Principal:
    principal = login(server, "dev")
    server.set_member(owner, alpha, "dev", Role.DEVELOPER)
    return principal


async def test_audit_of_the_workspace_examines_its_committed_branch(
    server: Server, alpha: uuid.UUID, dev: Principal
) -> None:
    box = Box(server, dev, alpha)
    info = box.ask("info")
    assert info["ok"] and info["project"] == "alpha" and info["user"] == "dev"
    assert "audit" in info["run_kinds"] and info["ai"]["enabled"] is False

    commit = box.commit("ai/feature", {"go.mod": "module example.com/x\n\ngo 1.22\n"})
    (box.repo / "uncommitted.txt").write_text("not examined\n", encoding="utf-8")
    answer = box.ask("audit", bundle="ai/feature", source="workspace", branch="ai/feature")
    assert answer["ok"], answer
    assert answer["commit"] == commit and answer["url"] == f"#/runs/{answer['run_id']}"
    with server.session() as s:
        run = s.get(Run, uuid.UUID(answer["run_id"]))
        assert run is not None and run.requested_by == "dev"
        source = Path(run.options["source"])
        assert run.goal == f"audit: workspace of dev: ai/feature @ {commit[:10]}"
        events = [
            e.target for e in s.query(AuditEvent).filter(AuditEvent.action == "workspace.agent")
        ]
    assert events == ["audit"]
    assert source == server.settings.sources_dir / str(alpha) / str(dev.user_id) / commit
    assert sorted(p.name for p in source.iterdir()) == ["go.mod", "main.py"]  # no .git, no draft

    # The run examines the snapshot, and keeps the project's state and configuration (a
    # discovery shows it without the audit's Docker sandbox).
    server.cancel(dev, uuid.UUID(answer["run_id"]), "not scanned in this test")
    probe = server.create_run(dev, alpha, "discover", {}, source=source)
    await Worker(server, poll_seconds=0.05).run_until_idle()
    assert server.run_status(dev, probe)["state"] == "COMPLETED"
    reports = [a for a in server.artifacts(dev, probe) if a["name"].endswith(".json")]
    contents = [server.artifact_content(dev, probe, uuid.UUID(a["id"]))[2] for a in reports]
    assert any(b'"go"' in c for c in contents)
    project_root = server.settings.allowed_roots[0] / "alpha"
    assert not (project_root / "go.mod").exists()
    assert (project_root / ".ai-coding" / "artifacts").is_dir()


def test_runs_are_followed_within_the_project_only(
    server: Server, owner: Principal, alpha: uuid.UUID, dev: Principal
) -> None:
    beta = server.create_project(owner, "beta", path=str(server.settings.allowed_roots[0] / "beta"))
    elsewhere = server.create_run(owner, beta, "discover", {})
    box = Box(server, dev, alpha)
    for op in ("status", "report", "cancel"):
        answer = box.ask(op, run_id=str(elsewhere))
        assert answer == {"ok": False, "error": "no such run in this project"}

    mine = server.create_run(dev, alpha, "discover", {})
    status = box.ask("status", run_id=str(mine))
    assert status["ok"] and status["state"] == "CREATED" and status["pending_approvals"] == []
    with server.session() as s:  # a step waits for a person (statuses are lower case)
        task = s.scalars(select(Task).where(Task.run_id == mine)).first()
        assert task is not None
        s.add(Approval(run_id=mine, task_id=task.id, operation="merge", status="pending",
                       requested_by="dev", reason="needs a maintainer"))  # fmt: skip
    status = box.ask("status", run_id=str(mine))
    assert status["pending_approvals"] == [{"operation": "merge", "reason": "needs a maintainer"}]
    report = box.ask("report", run_id=str(mine))
    assert report["ok"] and report["findings"] == [] and report["findings_by_severity"] == {}

    theirs = server.create_run(owner, alpha, "discover", {})
    assert "only runs of its own user" in box.ask("cancel", run_id=str(theirs))["error"]
    assert box.ask("cancel", run_id=str(mine))["state"] == "CANCELLED"
    assert box.ask("status", run_id="not-an-id")["error"] == "no such run in this project"


def test_tasks_for_the_orchestrator_and_the_limits(
    server: Server, alpha: uuid.UUID, dev: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Box(server, dev, alpha)
    answer = box.ask("start_task", kind="develop", params={"title": "Add tax"})
    assert not answer["ok"] and "no AI provider" in answer["error"]
    assert (
        "kind: one of develop, maintain" in box.ask("start_task", kind="audit", params={})["error"]
    )
    assert "unknown operation" in box.ask("approve", run_id="x")["error"]  # people approve
    assert "no bundle" in box.ask("audit", source="workspace", branch="main")["error"]
    assert "source:" in box.ask("audit", source="/etc")["error"]

    monkeypatch.setattr(agent_bridge, "RUNS_PER_HOUR", 1)
    assert box.ask("audit", source="project")["ok"]
    assert "too many requests" in box.ask("audit", source="project")["error"]


def test_only_members_with_a_workspace_right(
    server: Server, owner: Principal, alpha: uuid.UUID, dev: Principal
) -> None:
    box = Box(server, dev, alpha)
    server.set_member(owner, alpha, "dev", Role.AUDITOR)  # reads, has no workspace
    assert not box.ask("info")["ok"]
    server.set_member(owner, alpha, "dev", None)
    assert not box.ask("info")["ok"]


def test_the_mailbox_is_untrusted(
    server: Server, alpha: uuid.UUID, dev: Principal, tmp_path: Path
) -> None:
    box = Box(server, dev, alpha)
    secret = tmp_path / "secret.json"
    secret.write_text('{"op": "info"}', encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")

    rid = "a" * 32
    (box.path / f"{rid}.req").symlink_to(secret)  # a link: not followed
    (box.path / f"{rid}.res").symlink_to(victim)  # an answer through a link: replaced
    box.bridge.poll()
    answer = box.path / f"{rid}.res"
    assert not answer.is_symlink() and victim.read_text(encoding="utf-8") == "keep\n"
    assert json.loads(answer.read_text(encoding="utf-8"))["error"].startswith("bad request")

    rid = "b" * 32
    (box.path / f"{rid}.req").write_text("x" * (agent_bridge.REQUEST_LIMIT + 1), encoding="utf-8")
    box.bridge.poll()
    assert "too large" in json.loads((box.path / f"{rid}.res").read_text(encoding="utf-8"))["error"]

    rid = "c" * 32
    (box.path / f"{rid}.bundle").symlink_to(secret)
    (box.path / f"{rid}.req").write_text(json.dumps({"op": "audit", "branch": "main"}),
                                         encoding="utf-8")  # fmt: skip
    box.bridge.poll()
    error = json.loads((box.path / f"{rid}.res").read_text(encoding="utf-8"))["error"]
    assert error == "the bundle is not a regular file"

    # Only <hex id>.req names are requests.
    (box.path / "not-an-id.req").write_text('{"op": "info"}', encoding="utf-8")
    assert box.bridge.poll() == 0


async def test_the_api_serves_the_mailboxes(
    server: Server, alpha: uuid.UUID, dev: Principal
) -> None:
    box = Box(server, dev, alpha)
    stop = asyncio.Event()
    task = asyncio.create_task(AgentBridge(server, poll_seconds=0.05).run(stop))
    rid = uuid.uuid4().hex
    (box.path / f"{rid}.req").write_text('{"op": "info"}', encoding="utf-8")
    answer = box.path / f"{rid}.res"
    async with asyncio.timeout(10):
        while not answer.exists():
            await asyncio.sleep(0.05)
    stop.set()
    await task
    assert json.loads(answer.read_text(encoding="utf-8"))["project"] == "alpha"
    assert answer.stat().st_mode & 0o077 == 0
