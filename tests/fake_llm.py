"""In-memory fake of OpenAI-compatible and Anthropic LLM APIs. Test-only double.

Serves real HTTP on localhost so the production adapters, router, guard and the
CREATE workflow are exercised end-to-end without network access or real models.
The agent is recognised from the system prompt ("You are the <Title>"); each agent
answers with its sample output unless a script says otherwise.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests import agent_samples

DEFAULTS: dict[str, dict[str, Any]] = {
    "requirements": agent_samples.SPEC,
    "architect": agent_samples.ARCH,
    "planner": agent_samples.PLAN,
}
_AGENT = re.compile(
    r"You are the (Requirements|Architect|Planner|Developer|Reviewer|Database|Security|QA|"
    r"Performance|DevOps|Maintenance) Agent"
    r"( \(modernization\))?"
)
APPROVE: dict[str, Any] = {"summary": "Looks good.", "verdict": "approve", "findings": []}


@dataclass
class Error:
    """Scripted HTTP error."""

    status: int
    message: str = "scripted failure"


@dataclass
class FakeLLM:
    scripts: dict[str, list[dict[str, Any] | Error]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://localhost:{self.server.server_address[1]}"

    def agents_called(self) -> list[str]:
        return [r["agent"] for r in self.requests]

    def _answer(self, agent: str) -> dict[str, Any] | Error:
        script = self.scripts.get(agent)
        if script:
            return script.pop(0)
        if agent == "reviewer":
            return APPROVE
        if agent not in DEFAULTS:
            return Error(400, f"script for {agent} exhausted")
        return DEFAULTS[agent]

    def start(self) -> FakeLLM:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/chat/completions"):
                    system = next(m["content"] for m in body["messages"] if m["role"] == "system")
                    kind = "openai"
                elif self.path.endswith("/v1/messages"):
                    system = body.get("system", "")
                    kind = "anthropic"
                else:
                    self._send(404, {"error": {"message": "not found"}})
                    return
                match = _AGENT.search(system)
                agent = match.group(1).lower() if match else "unknown"
                if match and match.group(2):
                    agent = "migration"
                with fake._lock:
                    fake.requests.append({
                        "kind": kind, "agent": agent, "body": body,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    })  # fmt: skip
                    known = agent in DEFAULTS or agent in fake.scripts or agent == "reviewer"
                    answer = fake._answer(agent) if known else Error(400, f"no script: {agent}")
                if isinstance(answer, Error):
                    self._send(answer.status, {"error": {"message": answer.message}})
                    return
                text = json.dumps(answer, ensure_ascii=False)
                usage_in = sum(len(json.dumps(m)) for m in body.get("messages", [])) // 4
                if kind == "openai":
                    self._send(200, {
                        "model": body["model"],
                        "choices": [{"message": {"role": "assistant", "content": text},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": usage_in, "completion_tokens": len(text) // 4},
                    })  # fmt: skip
                else:
                    self._send(200, {
                        "model": body["model"], "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "submit_result",
                                     "input": answer}],
                        "usage": {"input_tokens": usage_in, "output_tokens": len(text) // 4},
                    })  # fmt: skip

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
