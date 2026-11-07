"""The chat daemon of workspace containers (deploy/workspace/ai-coding-agent, ADR-0037),
driven like the broker does — through its Unix socket — with fake `claude` and `codex`
CLIs that speak the real protocols (stream-json, app-server JSON-RPC)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "deploy" / "workspace" / "ai-coding-agent"
FAKES = ROOT / "tests" / "fixtures" / "agent-cli"
Event = dict[str, Any]


class Conn:
    """A client of the daemon (what `ai-coding-agent connect` relays)."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(str(path))
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(20)
        self.buffer = b""
        self.seen: list[Event] = []

    def send(self, **request: Any) -> None:
        self.sock.sendall(json.dumps(request).encode() + b"\n")

    def next(self, match: Callable[[Event], bool], timeout: float = 20) -> Event:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                event = json.loads(line)
                self.seen.append(event)
                if match(event):
                    return event
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buffer += chunk
        raise AssertionError(f"no matching event; seen {[e.get('t') for e in self.seen]}")

    def close(self) -> None:
        self.sock.close()


def kind(event: str, /, **fields: Any) -> Callable[[Event], bool]:
    """Matches an event by its kind and fields (``name`` is a field of tools and approvals)."""
    return lambda e: e.get("t") == event and all(e.get(k) == v for k, v in fields.items())


class Daemon:
    def __init__(self, tmp: Path) -> None:
        self.socket = tmp / "agent.sock"
        self.workdir = tmp / "ws"
        self.workdir.mkdir(exist_ok=True)
        self.cli_log = tmp / "cli.jsonl"
        self.env = {**os.environ, "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_CLI_LOG": str(self.cli_log),
                    "AI_CODING_AGENT_SOCKET": str(self.socket),
                    "AI_CODING_CHATS": str(tmp / "chats"),
                    "AI_CODING_WORKDIR": str(self.workdir), "HOME": str(tmp),
                    "AI_CODING_USAGE_INTERVAL": "0"}  # fmt: skip
        self.proc: subprocess.Popen[bytes] | None = None
        self.conns: list[Conn] = []

    def start(self) -> None:
        self.proc = subprocess.Popen([sys.executable, str(AGENT), "serve"], env=self.env,
                                     cwd=self.workdir)  # fmt: skip
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                Conn(self.socket).close()
            except OSError:
                time.sleep(0.05)
            else:
                return
        raise AssertionError("the daemon did not start")

    def kill(self) -> None:
        if self.proc is not None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)

    def connect(self) -> Conn:
        conn = Conn(self.socket)
        self.conns.append(conn)
        return conn

    def calls(self, cli: str) -> list[Event]:
        """What the daemon passed the fake CLIs (FAKE_CLI_LOG)."""
        if not self.cli_log.exists():
            return []
        lines = self.cli_log.read_text(encoding="utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e["cli"] == cli]

    def status(self) -> Event:
        out = subprocess.run([sys.executable, str(AGENT), "status"], env=self.env,
                             capture_output=True, text=True, check=True).stdout  # fmt: skip
        return dict(json.loads(out))


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[Daemon]:
    d = Daemon(tmp_path)
    d.start()
    yield d
    for conn in d.conns:
        conn.close()
    if d.proc is not None and d.proc.poll() is None:
        d.proc.terminate()
        d.proc.wait(10)


def new_chat(conn: Conn, agent: str, mode: str = "ask", workflow: str | None = None) -> str:
    conn.send(op="new", agent=agent, mode=mode, **({"workflow": workflow} if workflow else {}))
    return str(conn.next(kind("opened"))["meta"]["id"])


def flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_claude_chat_with_permission_and_history(daemon: Daemon) -> None:
    c = daemon.connect()
    c.send(op="hello")
    assert c.next(kind("hello"))["sessions"] == []
    auth = c.next(kind("auth"))["agents"]
    assert auth["claude"] == {"logged_in": True, "method": "api_key", "plan": None}
    assert auth["codex"]["logged_in"] is False
    sid = new_chat(c, "claude")
    c.send(op="send", session=sid, text="write a greeting")
    assert c.next(kind("user"))["text"] == "write a greeting"
    tool = c.next(kind("tool"))
    assert tool["title"] == "Записать hello.txt"
    approval = c.next(kind("approval"))
    assert approval["session_option"] is True
    c.send(op="approve", session=sid, id=approval["id"], decision="allow")
    assert c.next(kind("approval_done"))["decision"] == "allow"
    result = c.next(kind("tool_result"))
    assert result["ok"] is True and result["file"] == "hello.txt" and result["diff"] == "+hello"
    assert c.next(kind("message"))["text"] == "Записал файл."
    assert c.next(kind("turn", state="done"))["cost"] == 0.01
    assert (daemon.workdir / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    c.close()

    # A new client: the chat is listed (titled by its first message) and replayed.
    c = daemon.connect()
    c.send(op="hello")
    (meta,) = c.next(kind("hello"))["sessions"]
    assert meta["title"] == "write a greeting" and meta["native"]
    c.send(op="open", session=sid)
    history = c.next(kind("opened"))["history"]
    assert [e["t"] for e in history] == ["user", "tool", "approval", "approval_done",
                                         "tool_result", "message", "turn"]  # fmt: skip

    # Denied: nothing is written, the agent is told.
    (daemon.workdir / "hello.txt").unlink()
    c.send(op="send", session=sid, text="write again")
    approval = c.next(kind("approval"))
    c.send(op="approve", session=sid, id=approval["id"], decision="deny")
    assert c.next(kind("tool_result"))["ok"] is False
    c.next(kind("turn", state="done"))
    assert not (daemon.workdir / "hello.txt").exists()


def test_the_agent_keeps_working_without_a_browser(daemon: Daemon) -> None:
    c = daemon.connect()
    sid = new_chat(c, "claude")
    c.send(op="send", session=sid, text="write while I am away")
    approval = c.next(kind("approval"))
    c.close()  # the browser tab is closed
    time.sleep(0.2)
    assert daemon.status()["busy"] is True  # the workspace container must not be reaped

    c = daemon.connect()
    c.send(op="open", session=sid)
    opened = c.next(kind("opened"))
    assert opened["busy"] is True
    assert abs(opened["now"] - time.time()) < 60  # the panel times running actions by this clock
    assert approval["id"] in [e.get("id") for e in opened["history"] if e["t"] == "approval"]
    c.send(op="approve", session=sid, id=approval["id"], decision="allow_session")
    c.next(kind("turn", state="done"))
    assert (daemon.workdir / "hello.txt").exists()
    assert daemon.status()["busy"] is False


def test_interrupt_and_restart(daemon: Daemon) -> None:
    c = daemon.connect()
    sid = new_chat(c, "claude")
    c.send(op="send", session=sid, text="slow task")
    assert c.next(kind("delta"))["text"] == "Думаю"  # streamed, not recorded
    c.send(op="send", session=sid, text="another")  # one turn at a time
    assert "still working" in c.next(kind("error"))["message"]
    c.send(op="interrupt", session=sid)
    c.next(kind("turn", state="interrupted"))

    # A turn cut by a daemon restart is closed in the history; the chat resumes.
    c.send(op="send", session=sid, text="slow again")
    c.next(kind("delta"))
    c.close()
    daemon.kill()
    daemon.start()
    c = daemon.connect()
    c.send(op="open", session=sid)
    history = c.next(kind("opened"))["history"]
    assert history[-1]["t"] == "turn" and history[-1]["state"] == "interrupted"
    assert "delta" not in {e["t"] for e in history}
    c.send(op="send", session=sid, text="hello")
    assert c.next(kind("message"))["text"] == "Привет! (resumed)"


def test_codex_chat_with_approval_and_device_login(daemon: Daemon) -> None:
    c = daemon.connect()
    c.send(op="login", agent="codex")
    login = c.next(kind("login"))
    assert login["url"] == "https://auth.openai.com/codex/device" and login["code"] == "ABCD-1234"
    # The fake completes the sign-in at once; a person takes longer: either order is fine.
    done = [e for e in c.seen if e["t"] == "login_done"] or [c.next(kind("login_done"))]
    assert done[0]["success"] is True
    c.send(op="login", agent="claude")  # Claude Code: sign-in is the official CLI's own
    assert "terminal" in c.next(kind("error"))["message"]

    sid = new_chat(c, "codex")
    c.send(op="send", session=sid, text="list the files")
    assert c.next(kind("tool"))["title"] == "$ ls -la"
    approval = c.next(kind("approval"))
    assert approval["title"] == "$ ls -la" and approval["detail"] == "list files"
    c.send(op="approve", session=sid, id=approval["id"], decision="allow_session")
    result = c.next(kind("tool_result"))
    assert result["ok"] is True and result["text"] == "total 0\n"
    assert c.next(kind("delta"))["text"] == "Гото"
    assert c.next(kind("message"))["text"] == "Готово"
    assert c.next(kind("turn", state="done"))["duration_ms"] == 7


def test_audit_mode_only_reads(daemon: Daemon) -> None:
    """AUDIT: whatever permission mode the user picks, Claude Code runs without its writing
    tools; what only reads goes through, any other permission request is refused (the chat
    is told), and a file changed anyway is reported after the turn."""
    subprocess.run(["git", "init", "-q", str(daemon.workdir)], check=True)
    c = daemon.connect()
    sid = new_chat(c, "claude", mode="full", workflow="audit")
    c.send(op="send", session=sid, text="write the fix")
    notice = c.next(kind("notice"))
    assert "Аудит" in notice["text"] and "hello.txt" in notice["text"]
    assert c.next(kind("tool_result"))["ok"] is False
    c.next(kind("turn", state="done"))
    assert not (daemon.workdir / "hello.txt").exists()
    assert not [e for e in c.seen if e["t"] == "approval"]
    (call,) = daemon.calls("claude")
    args = call["args"]
    assert flag(args, "--permission-mode") == "default"
    assert args[args.index("--disallowedTools") + 1:][:4] == ["Write", "Edit", "MultiEdit",
                                                               "NotebookEdit"]  # fmt: skip
    assert "MODE: AUDIT" in flag(args, "--append-system-prompt")
    assert "--allow-dangerously-skip-permissions" not in args
    # The platform's tools: its read-only MCP server; its read tools need no approval.
    (server,) = json.loads(flag(args, "--mcp-config"))["mcpServers"].values()
    assert server["args"][-2:] == ["mcp", "--read-only"] and server["args"][0] == str(AGENT)
    assert args[args.index("--allowedTools") + 1:][:4] == [
        "mcp__platform__info", "mcp__platform__audit", "mcp__platform__run_status",
        "mcp__platform__run_report"]  # fmt: skip
    assert daemon.calls("claude")[0] == call  # no mode switch: still read-only

    c.send(op="send", session=sid, text="read the notes")  # reading needs no question
    c.next(kind("turn", state="done"))
    answers = [e["answer"]["behavior"] for e in daemon.calls("claude") if "answer" in e]
    assert answers == ["allow", "allow"]

    c.send(op="send", session=sid, text="sneak a change")
    c.next(kind("turn", state="done"))
    warning = c.next(kind("error"))
    assert "Аудит" in warning["message"] and "sneaky.txt" in warning["message"]


def test_modes_that_change_code_start_with_an_agreed_plan(daemon: Daemon) -> None:
    """DEVELOP (and CREATE, MODERNIZE, MAINTAIN): Claude Code starts in its plan mode; the
    plan comes to the chat for approval; once approved the chat goes on in the permission
    mode the user picked, and stays agreed."""
    c = daemon.connect()
    sid = new_chat(c, "claude", mode="edits", workflow="develop")
    c.send(op="send", session=sid, text="plan the greeting")
    approval = c.next(kind("approval"))
    assert approval["name"] == "ExitPlanMode" and approval["title"] == "План работы"
    assert "Добавить hello.txt" in approval["detail"]  # from ~/.claude/plans/
    c.send(op="approve", session=sid, id=approval["id"], decision="allow")
    c.next(kind("planned"))
    assert c.next(kind("message"))["text"] == "Выполняю план."
    c.next(kind("turn", state="done"))
    (call,) = daemon.calls("claude")[:1]
    assert flag(call["args"], "--permission-mode") == "plan"
    assert "--allow-dangerously-skip-permissions" in call["args"]  # "full" after the plan
    assert (
        json.loads(flag(call["args"], "--mcp-config"))["mcpServers"]["platform"]["args"][-1]
        == "mcp"
    )
    assert "PLATFORM TOOLS" in flag(call["args"], "--append-system-prompt")
    assert "MODE: DEVELOP" in flag(call["args"], "--append-system-prompt")
    answer = next(e["answer"] for e in daemon.calls("claude") if "answer" in e)
    assert answer["updatedPermissions"] == [{"type": "setMode", "mode": "acceptEdits",
                                             "destination": "session"}]  # fmt: skip

    # Agreed: the next turns run in the user's mode (here they switch to "ask").
    c.send(op="send", session=sid, text="hello", mode="ask")
    c.next(kind("turn", state="done"))
    assert {"cli": "claude", "set_mode": "default"} in daemon.calls("claude")
    c.send(op="open", session=sid)
    assert c.next(kind("opened"))["meta"]["planned"] is True

    # A rejected plan: the agent is told to revise it; the chat is still planning.
    sid = new_chat(c, "claude", workflow="maintain")
    c.send(op="send", session=sid, text="plan a fix")
    approval = c.next(kind("approval"))
    c.send(op="approve", session=sid, id=approval["id"], decision="deny")
    assert c.next(kind("message"))["text"] == "Жду правок."
    answer = [e["answer"] for e in daemon.calls("claude") if "answer" in e][-1]
    assert answer["behavior"] == "deny" and "revised plan" in answer["message"]
    c.send(op="open", session=sid)
    assert "planned" not in c.next(kind("opened"))["meta"]


def test_codex_follows_the_mode(daemon: Daemon) -> None:
    """Codex has no plan mode: its developer instructions carry the mode; until the plan is
    approved from the chat every change needs an approval; AUDIT runs the commands that
    only read and declines the rest."""
    c = daemon.connect()
    sid = new_chat(c, "codex", mode="full", workflow="audit")
    c.send(op="send", session=sid, text="check the code")  # ls -la: reads
    assert c.next(kind("message"))["text"] == "Готово"
    c.next(kind("turn", state="done"))
    c.send(op="send", session=sid, text="remove the build")  # rm -rf build: declined
    assert "«$ rm -rf build» не выполнено" in c.next(kind("notice"))["text"]
    assert c.next(kind("message"))["text"] == "Отменено"
    c.next(kind("turn", state="done"))
    assert not [e for e in c.seen if e["t"] == "approval"]
    start, turn, _ = daemon.calls("codex")
    assert "MODE: AUDIT" in start["params"]["developerInstructions"]
    platform = start["params"]["config"]["mcp_servers"]["platform"]
    assert platform["args"][-2:] == ["mcp", "--read-only"]
    assert turn["params"]["approvalPolicy"] == "untrusted"  # not "never", despite "full"

    # Planning in "full": what only reads is not asked (ls -la), a change is.
    sid = new_chat(c, "codex", mode="full", workflow="modernize")
    c.send(op="send", session=sid, text="propose the steps")
    assert c.next(kind("tool_result"))["ok"] is True
    c.next(kind("turn", state="done"))
    assert not [e for e in c.seen if e["t"] == "approval"]
    c.send(op="send", session=sid, text="remove the build")
    c.send(op="approve", session=sid, id=c.next(kind("approval"))["id"], decision="deny")
    c.next(kind("turn", state="done"))
    assert daemon.calls("codex")[-1]["params"]["approvalPolicy"] == "untrusted"
    c.send(op="send", session=sid, text="План утверждён", approve_plan=True)
    c.next(kind("planned"))
    c.send(op="approve", session=sid, id=c.next(kind("approval"))["id"], decision="allow")
    c.next(kind("turn", state="done"))
    assert daemon.calls("codex")[-1]["params"]["approvalPolicy"] == "never"
    assert "MODE: MODERNIZE" in daemon.calls("codex")[3]["params"]["developerInstructions"]

    # Planning in "ask": the user wants every command asked, reads included.
    sid = new_chat(c, "codex", mode="ask", workflow="develop")
    c.send(op="send", session=sid, text="look around")
    asked = c.next(kind("approval"))
    assert asked["title"] == "$ ls -la"
    c.send(op="approve", session=sid, id=asked["id"], decision="deny")
    c.next(kind("turn", state="done"))


def load_agent() -> Any:
    """The daemon script as a module (its pure helpers)."""
    import importlib.machinery  # noqa: PLC0415
    import importlib.util  # noqa: PLC0415

    loader = importlib.machinery.SourceFileLoader("ai_coding_agent", str(AGENT))
    spec = importlib.util.spec_from_loader("ai_coding_agent", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


READS = ["cat src/a.py", "sed -n '1,120p' src/a.py", "grep -rn 'def ' src | head -50",
         "rg -n TODO --glob '*.py'", "find . -name '*.py' -not -path './.git/*' | wc -l",
         "git log --oneline -20", "git diff HEAD~1 -- src", "ls -la && cat README.md",
         "find . -type f 2>/dev/null | head", "find src -name '*.py' | xargs grep -n eval",
         "bash -lc 'cat a.txt'", "/usr/bin/bash -lc 'git status --short && cat Makefile'",
         "sed -n '/def x/,/^$/p' f.py", "git branch -a",
         "cd web && ls src", "wc -l < f.txt", "grep x f 2>&1 | head"]  # fmt: skip
WRITES = ["rm -rf build", "cat a > b", "echo x >> f", "sed -i 's/a/b/' f", "find . -delete",
          "find . -exec rm {} ;", "git commit -m x", "git diff --output=x", "rg --pre ./x foo",
          "sort -o out in", "uniq a b", "cat a; rm b", "cat a\nrm b", "cat `rm b`",
          "cat $(rm b)", "python -c 'print(1)'", "npm test", "tee f", "curl http://x",
          "find . | xargs rm", "awk '{print}' f", "PATH=/tmp cat x", "cat a &",
          "git -c core.pager=sh log", "git branch -D x", "sed 's/a/b/w out' f",
          "bash -lc 'rm x'", "/usr/bin/bash -lc 'cat a > b'", "sed -n -e 1p -e 'w x' f",
          "git push",
          "cat 'unterminated"]  # fmt: skip


@pytest.mark.parametrize("command", READS)
def test_audit_lets_reading_commands_through(command: str) -> None:
    assert load_agent().read_only_command(command) is True


@pytest.mark.parametrize("command", WRITES)
def test_audit_refuses_any_other_command(command: str) -> None:
    assert load_agent().read_only_command(command) is False


def test_codex_mcp_questions_come_to_the_chat(daemon: Daemon) -> None:
    """Codex asks before an MCP tool call (elicitation): a card in the chat; in AUDIT the
    platform's own read-only server is let through, any other server refused."""
    c = daemon.connect()
    sid = new_chat(c, "codex", workflow="develop")
    c.send(op="send", session=sid, text="elicit platform")
    approval = c.next(kind("approval", name="mcp"))
    assert approval["title"] == "MCP «platform»: Allow start_task?"
    c.send(op="approve", session=sid, id=approval["id"], decision="allow")
    c.next(kind("turn", state="done"))
    sid = new_chat(c, "codex", workflow="audit")
    c.send(op="send", session=sid, text="elicit platform")
    c.next(kind("turn", state="done"))
    c.send(op="send", session=sid, text="elicit github")
    assert "«github» отклонён" in c.next(kind("notice"))["text"]
    c.next(kind("turn", state="done"))
    answers = [e["elicitation"] for e in daemon.calls("codex") if "elicitation" in e]
    assert answers == [{"action": "accept", "content": {}}, {"action": "accept", "content": {}},
                       {"action": "decline", "content": None}]  # fmt: skip


class Platform:
    """The API's side of a mailbox, for the MCP server's tests: answers each request."""

    def __init__(self, box: Path, answer: Callable[[Event, Path], Event]) -> None:
        self.box, self.answer = box, answer
        self.requests: list[Event] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while not self.stop.is_set():
            for req in self.box.glob("*.req"):
                rid = req.name.split(".")[0]
                request = json.loads(req.read_text(encoding="utf-8"))
                req.unlink()
                self.requests.append(request)
                (self.box / f".{rid}.tmp").write_text(
                    json.dumps(self.answer(request, self.box / f"{rid}.bundle")), encoding="utf-8"
                )
                (self.box / f".{rid}.tmp").replace(self.box / f"{rid}.res")
            time.sleep(0.02)


class Mcp:
    """``ai-coding-agent mcp`` driven as Claude Code / Codex drive it (stdio JSON-RPC)."""

    def __init__(self, env: dict[str, str], *args: str) -> None:
        argv = [sys.executable, str(AGENT), "mcp", *args]
        self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)  # fmt: skip
        self.seq = 0

    def call(self, method: str, params: Event | None = None) -> Event:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.seq += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.seq, "method": method,
                                          "params": params or {}}) + "\n")  # fmt: skip
        self.proc.stdin.flush()
        reply: Event = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.seq
        return reply

    def tool(self, name: str, **arguments: Any) -> tuple[bool, str]:
        result = self.call("tools/call", {"name": name, "arguments": arguments})["result"]
        return not result["isError"], result["content"][0]["text"]

    def close(self) -> None:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.close()
        self.proc.wait(10)
        self.proc.stdout.close()


def test_platform_mcp_server(tmp_path: Path, git_env: None) -> None:
    """The tools reach the platform through the mailbox: an audit of the workspace sends
    its branch as a bundle; run_status waits; AUDIT chats get the read-only tools only."""
    box, work = tmp_path / "platform", tmp_path / "ws"
    box.mkdir()
    work.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"],
                 ["checkout", "-q", "-b", "ai/feature"]):  # fmt: skip
        subprocess.run(["git", *args], cwd=work, check=True)
    (work / "draft.txt").write_text("x", encoding="utf-8")
    states = iter(["RUNNING", "COMPLETED"])

    def answer(request: Event, bundle: Path) -> Event:
        if request["op"] == "audit":
            assert bundle.is_file() and bundle.stat().st_size > 0  # sent with the request
            return {"ok": True, "run_id": "r1", "url": "#/runs/r1", "branch": request["branch"]}
        if request["op"] == "status":
            return {"ok": True, "run_id": "r1", "state": next(states), "pending_approvals": []}
        return {"ok": False, "error": f"no {request['op']} here"}

    env = {**os.environ, "AI_CODING_PLATFORM_MAILBOX": str(box), "AI_CODING_WORKDIR": str(work)}
    platform = Platform(box, answer)
    mcp = Mcp(env)
    try:
        init = mcp.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        assert init["result"]["serverInfo"]["name"] == "ai-coding-platform"
        tools = {t["name"]: t for t in mcp.call("tools/list")["result"]["tools"]}
        assert set(tools) == {
            "info",
            "audit",
            "run_status",
            "run_report",
            "start_task",
            "cancel_run",
        }
        assert tools["audit"]["annotations"]["readOnlyHint"] is True
        assert tools["start_task"]["annotations"]["readOnlyHint"] is False
        ok, text = mcp.tool("audit")
        assert ok and json.loads(text)["branch"] == "ai/feature"
        assert "1 uncommitted change(s)" in json.loads(text)["note"]
        ok, text = mcp.tool("run_status", run_id="r1", wait_seconds=30)
        assert ok and json.loads(text)["state"] == "COMPLETED"
        assert [r["op"] for r in platform.requests] == ["audit", "status", "status"]
        ok, text = mcp.tool("start_task", kind="develop", params={"title": "x"})
        assert not ok and text == "Platform error: no start_task here"
        assert mcp.call("bogus")["error"]["code"] == -32601
        assert sorted(p.name for p in box.iterdir()) == []  # nothing left in the mailbox
    finally:
        mcp.close()
        platform.stop.set()


def test_platform_mcp_server_limits(tmp_path: Path) -> None:
    """AUDIT chats get the read-only tools only; without a mailbox the agent is told why."""
    env = {
        **os.environ,
        "AI_CODING_PLATFORM_MAILBOX": str(tmp_path),
        "AI_CODING_WORKDIR": str(tmp_path),
    }
    read_only = Mcp(env, "--read-only")
    try:
        names = {t["name"] for t in read_only.call("tools/list")["result"]["tools"]}
        assert names == {"info", "audit", "run_status", "run_report"}
        assert read_only.tool("start_task", kind="develop", params={}) == (
            False,
            "unknown tool 'start_task'",
        )
    finally:
        read_only.close()

    gone = Mcp({**env, "AI_CODING_PLATFORM_MAILBOX": str(tmp_path / "none")})
    try:
        ok, text = gone.tool("info")
        assert not ok and "restart the environment" in text
    finally:
        gone.close()


def test_bad_requests_are_answered(daemon: Daemon) -> None:
    c = daemon.connect()
    c.sock.sendall(b"not json\n")
    c.next(kind("error"))
    bad = ({"op": "dance"}, {"op": "open", "session": "../etc"}, {"op": "new", "agent": "gpt"},
           {"op": "new", "agent": "claude", "workflow": "hack"},
           {"op": "approve", "session": "x", "id": 1}, {"op": "delete", "session": "nope"},
           {"op": "usage", "agent": "gpt"})  # fmt: skip
    for request in bad:
        c.send(**request)
        c.next(kind("error"))
    sid = new_chat(c, "claude")
    c.send(op="send", session=sid, text="   ")
    assert "empty" in c.next(kind("error"))["message"]
    c.send(op="approve", session=sid, id="nope", decision="allow")
    assert "no longer pending" in c.next(kind("error"))["message"]


def test_tabs_close_and_delete(daemon: Daemon) -> None:
    """The panel's tabs: a client follows every chat it opened until it closes one (the
    agent goes on); a chat is deleted for every client with its files, never mid-turn."""
    c = daemon.connect()
    c.send(op="hello")
    assert c.next(kind("hello"))["features"] == ["tabs", "delete", "usage"]
    c.send(op="new", agent="claude", ref="new:1")  # the tab that asked for the chat
    first = c.next(kind("opened"))
    assert first["ref"] == "new:1"
    s1, s2 = str(first["meta"]["id"]), new_chat(c, "claude")
    c.send(op="send", session=s1, text="hello")  # the earlier chat is still followed
    assert c.next(kind("message", session=s1))["text"] == "Привет! (new)"
    c.next(kind("turn", session=s1, state="done"))

    c.send(op="close", session=s1)
    other = daemon.connect()
    other.send(op="open", session=s1)
    other.next(kind("opened"))
    mark = len(c.seen)
    other.send(op="send", session=s1, text="hello again")
    other.next(kind("message", session=s1))
    other.next(kind("turn", session=s1, state="done"))
    c.send(op="status")
    c.next(kind("status"))
    assert not [e for e in c.seen[mark:] if e.get("session") == s1]

    c.send(op="send", session=s2, text="slow task")
    c.next(kind("delta", session=s2))
    c.send(op="delete", session=s2)
    assert "ещё работает" in c.next(kind("error"))["message"]
    c.send(op="interrupt", session=s2)
    c.next(kind("turn", session=s2, state="interrupted"))
    c.send(op="delete", session=s2)
    assert c.next(kind("deleted"))["session"] == s2
    assert other.next(kind("deleted"))["session"] == s2  # every panel drops it
    assert not list(Path(daemon.env["AI_CODING_CHATS"]).glob(f"{s2}*"))
    c.send(op="hello")
    assert [m["id"] for m in c.next(kind("hello"))["sessions"]] == [s1]
    c.send(op="open", session=s2)
    assert "no such chat" in c.next(kind("error"))["message"]


def at(*parts: int) -> float:
    return datetime(*parts, tzinfo=UTC).timestamp()  # type: ignore[arg-type, misc]


def test_plan_limits(daemon: Daemon) -> None:
    """The plan's limits (5-hour window, week) from the official CLIs: Claude Code's own
    /usage (a local command: no model call, no session kept) and Codex's
    account/rateLimits/read; served from memory, read again on request and after a turn."""
    c = daemon.connect()
    c.send(op="usage", agent="claude")
    usage = c.next(kind("usage", agent="claude"))
    assert usage["limits"] == [
        {"window": "session", "minutes": 300, "scope": None, "percent": 4,
         "resets_at": at(2099, 11, 6, 23, 19, 59, 833351), "severity": "normal"},
        {"window": "week", "minutes": 10080, "scope": None, "percent": 58,
         "resets_at": at(2099, 11, 8, 22, 59, 59), "severity": "warning"},
        {"window": "week", "minutes": 10080, "scope": "Fable", "percent": 3,
         "resets_at": at(2099, 11, 8, 22, 59, 59), "severity": "normal"},
    ]  # fmt: skip
    assert "Current session: 4% used" in usage["text"]
    (call,) = [e for e in daemon.calls("claude") if "usage" in e]
    assert (
        call["usage"][:2] == ["-p", "--output-format"]
        and "--no-session-persistence" in call["usage"]
    )
    assert call["cwd"] == daemon.env["HOME"]  # not the project: none of its settings or hooks

    c.send(op="usage", agent="claude")  # fresh: from memory
    assert c.next(kind("usage", agent="claude"))["at"] == usage["at"]
    assert len([e for e in daemon.calls("claude") if "usage" in e]) == 1
    c.send(op="usage", agent="claude", refresh=True)
    assert c.next(kind("usage", agent="claude"))["at"] > usage["at"]
    sid = new_chat(c, "claude")
    c.send(op="send", session=sid, text="hello")
    c.next(kind("turn", state="done"))
    c.next(kind("usage", agent="claude"))  # the turn used the plan: read again
    assert len([e for e in daemon.calls("claude") if "usage" in e]) >= 3

    c.send(op="usage", agent="codex")
    codex = c.next(kind("usage", agent="codex"))
    assert codex["plan"] == "plus" and codex["reached"] is None
    assert codex["limits"] == [
        {"window": "session", "minutes": 300, "scope": None, "percent": 2,
         "resets_at": 4100000000.0, "severity": None},
        {"window": "week", "minutes": 10080, "scope": None, "percent": 40,
         "resets_at": 4100500000.0, "severity": None},
    ]  # fmt: skip
    sid = new_chat(c, "codex")
    c.send(op="send", session=sid, text="list the files")
    c.send(op="approve", session=sid, id=c.next(kind("approval"))["id"], decision="allow")
    c.next(kind("turn", state="done"))
    assert c.next(kind("usage", agent="codex"))["limits"][0]["percent"] == 7


@pytest.mark.parametrize(
    ("account", "expected"), [("api", "Total cost"), ("fail", "Not logged in")]
)
def test_plan_limits_without_a_subscription(tmp_path: Path, account: str, expected: str) -> None:
    """An API-key account has no plan limits: the CLI's own text says what applies; an
    error comes to the panel instead of the limits."""
    d = Daemon(tmp_path)
    d.env["FAKE_CLAUDE_USAGE"] = account
    d.start()
    try:
        c = d.connect()
        c.send(op="usage", agent="claude")
        usage = c.next(kind("usage", agent="claude"))
        assert usage["limits"] == []
        assert expected in (usage.get("error") if account == "fail" else usage["text"])
        c.close()
    finally:
        assert d.proc is not None
        d.proc.terminate()
        d.proc.wait(10)


class ContainerChat:
    """The chat of a real workspace container, as the API's WebSocket relays it."""

    def __init__(self, chat: Any) -> None:
        self.chat = chat
        self.events = chat.output()
        self.seen: list[Event] = []

    async def request(self, **body: Any) -> None:
        await self.chat.send(json.dumps(body).encode())

    async def until(self, event: str, /, timeout: float = 120, **fields: Any) -> Event:
        import asyncio  # noqa: PLC0415

        async def wait() -> Event:
            async for line in self.events:
                parsed: Event = json.loads(line)
                self.seen.append(parsed)
                if kind(event, **fields)(parsed):
                    return parsed
            raise AssertionError(f"chat closed; seen {[e.get('t') for e in self.seen]}")

        try:
            return await asyncio.wait_for(wait(), timeout)
        except TimeoutError:
            seen = [(e.get("t"), e.get("name") or e.get("state")) for e in self.seen]
            raise AssertionError(f"no {event} {fields}; seen {seen}") from None

    async def new(self, workflow: str) -> str:
        await self.request(op="new", agent="claude", mode="ask", workflow=workflow)
        return str((await self.until("opened"))["meta"]["id"])


def project_and_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A project repository, the user's workspace clone of it, the user's home."""
    project = tmp_path / "project"
    project.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    repo = tmp_path / "ws" / "repo"
    subprocess.run(["git", "clone", "-q", str(project), str(repo)], check=True)
    home = tmp_path / "user-home"
    (home / ".claude").mkdir(parents=True)
    return project, repo, home


@dataclass
class RealWorkspace:
    """A workspace container of the real image, driven by the broker (needs Docker)."""

    broker: Any
    ref: Any
    repo: Path
    home: Path
    gateway: str  # where the container reaches this host

    def use_fake_api(self, url: str) -> None:
        """Claude Code's own settings point it at the fake API (no account needed)."""
        env = {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": "sk-ant-fake"}
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}),
                                                             encoding="utf-8")  # fmt: skip


def real_workspace(tmp_path: Path) -> RealWorkspace:
    import uuid  # noqa: PLC0415

    from ai_coding.api.settings import ServerSettings  # noqa: PLC0415
    from ai_coding.api.workspace_broker import LocalBroker, WorkspaceRef  # noqa: PLC0415
    from tests.integration.conftest import require_images  # noqa: PLC0415

    require_images("ai-coding-workspace:local")
    gateway = subprocess.run(["docker", "network", "inspect", "bridge", "--format",
                              "{{(index .IPAM.Config 0).Gateway}}"],
                             capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    project, repo, home = project_and_clone(tmp_path)
    settings = ServerSettings(data_dir=tmp_path / "data", database_url="sqlite://",
                              workspace_egress=("*",))  # fmt: skip
    ref = WorkspaceRef(uuid.uuid4(), uuid.uuid4(), "dev", project, repo, home)
    return RealWorkspace(LocalBroker(settings), ref, repo, home, gateway)


def test_real_container_platform_tools(tmp_path: Path, git_env: None) -> None:
    """The real Claude Code calls the platform's MCP tools without asking, in AUDIT and
    while planning; the request reaches the API's side of the mailbox (a stand-in here)."""
    import asyncio  # noqa: PLC0415

    from tests.fake_anthropic import FakeAnthropic  # noqa: PLC0415

    ws = real_workspace(tmp_path)
    box = ws.ref.mailbox

    def answer(request: Event, _bundle: Path) -> Event:
        return {"ok": True, "project": "alpha", "op": request["op"], "run_kinds": ["audit"]}

    async def scenario(url: str) -> ContainerChat:
        ws.use_fake_api(url)
        c = ContainerChat(await ws.broker.chat(ws.ref))
        for workflow in ("audit", "develop"):  # develop: still in plan mode
            sid = await c.new(workflow)
            await c.request(op="send", session=sid, text="platform info please")
            await c.until("tool_result", session=sid)
            await c.until("turn", state="done")
        await c.chat.close()
        return c

    platform: Platform | None = None
    try:
        with FakeAnthropic(host="0.0.0.0") as fake:  # noqa: S104 - reached from the container
            box.mkdir(parents=True, exist_ok=True)
            platform = Platform(box, answer)
            c = asyncio.run(scenario(f"http://{ws.gateway}:{fake.url.rsplit(':', 1)[1]}"))
    finally:
        if platform is not None:
            platform.stop.set()
        asyncio.run(ws.broker.stop(ws.ref))
    tools = [e for e in c.seen if e["t"] == "tool"]
    assert [e["title"] for e in tools] == ["Платформа: сведения о проекте"] * 2
    results = [e for e in c.seen if e["t"] == "tool_result"]
    assert all(r["ok"] and '"project": "alpha"' in r["text"] for r in results), results
    assert not [e for e in c.seen if e["t"] in ("approval", "notice")]
    assert [r["op"] for r in platform.requests] == ["info", "info"]


def test_real_container_chat(tmp_path: Path, git_env: None) -> None:
    """With Docker and the workspace image: the broker starts the daemon in the user's
    container, and the real Claude Code CLI (against a fake Anthropic API) works in the
    chat's mode: AUDIT without writing tools; DEVELOP plans first (ExitPlanMode, approved
    in the chat), then asks for and gets a permission and writes into the workspace."""
    import asyncio  # noqa: PLC0415

    from tests.fake_anthropic import FakeAnthropic  # noqa: PLC0415

    ws = real_workspace(tmp_path)
    broker, ref, repo = ws.broker, ws.ref, ws.repo

    async def scenario(url: str) -> tuple[ContainerChat, str]:
        ws.use_fake_api(url)
        c = ContainerChat(await broker.chat(ref))
        audit = await c.new("audit")
        await c.request(op="send", session=audit, text="please write a file")
        await c.until("turn", state="done")

        sid = await c.new("develop")
        await c.request(op="send", session=sid, text="please plan it")
        plan = await c.until("approval")
        assert plan["name"] == "ExitPlanMode", plan
        await c.request(op="approve", session=sid, id=plan["id"], decision="allow")
        await c.until("planned")
        await c.until("turn", state="done")
        await c.request(op="send", session=sid, text="please write a file")
        approval = await c.until("approval", name="Write")
        await c.request(op="approve", session=sid, id=approval["id"], decision="allow")
        await c.until("turn", state="done")
        await c.request(op="send", session=sid, text="please write another file", mode="full")
        await c.until("turn", state="done")
        await c.chat.close()
        return c, sid

    try:
        with FakeAnthropic(host="0.0.0.0") as fake:  # noqa: S104 - reached from the container
            port = fake.url.rsplit(":", 1)[1]
            c, sid = asyncio.run(scenario(f"http://{ws.gateway}:{port}"))
    finally:
        asyncio.run(broker.stop(ref))
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert (repo / "second.txt").read_text(encoding="utf-8") == "hello\n"  # full: not asked
    # The language instruction and the mode reach the model through Claude Code's option;
    # in AUDIT the model is not even offered the writing tools.
    systems = [json.dumps(r["body"].get("system")) for r in fake.requests]
    assert all("language of the user's most recent message" in s for s in systems if "MODE:" in s)
    audit_tools = {tool["name"] for r, s in zip(fake.requests, systems, strict=True)
                   if "MODE: AUDIT" in s for tool in r["body"].get("tools") or []}  # fmt: skip
    assert "Read" in audit_tools and not audit_tools & {"Write", "Edit", "NotebookEdit"}
    assert any("MODE: DEVELOP" in s for s in systems)
    # Nothing was asked in AUDIT or in full access: only the plan and the first write.
    assert [e["name"] for e in c.seen if e["t"] == "approval"] == ["ExitPlanMode", "Write"]
    tool = next(e for e in c.seen if kind("tool", name="Write", session=sid)(e))
    assert tool["title"] == "Записать hello.txt"
    result = next(e for e in c.seen if e["t"] == "tool_result" and e["id"] == tool["id"])
    assert result["ok"] is True and result["diff"] == "+hello"
