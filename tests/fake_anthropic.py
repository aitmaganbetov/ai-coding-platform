"""A streaming (SSE) fake of the Anthropic Messages API, for driving the real Claude Code
CLI in tests without an account (``ANTHROPIC_BASE_URL``). Test-only double.

Script, per request (the last user turn decides):
* it carries a ``tool_result`` → a short text answer ("Готово: <result>");
* the user text contains ``bash`` → a ``Bash`` tool call (``echo FAKE-$((6*7))``);
* the user text contains ``platform`` → the platform's ``mcp__platform__info``;
* the user text contains ``plan`` → ``ExitPlanMode`` (plan mode's "the plan is ready");
* the user text contains ``write`` → a ``Write`` tool call (``hello.txt``; with
  ``another``, ``second.txt``);
* otherwise → a text answer echoing the prompt.
"""

from __future__ import annotations

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _tool_result(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                return " ".join(b.get("text", "") for b in inner if b.get("type") == "text")
            return str(inner)
    return None


def plan(body: dict[str, Any]) -> tuple[str, Any]:
    """("text", str) or ("tool", (name, input)) for a Messages request."""
    messages = [m for m in body.get("messages", []) if m.get("role") == "user"]
    last = messages[-1] if messages else {}
    content = last.get("content")
    # Claude Code adds its own <system-reminder> text blocks next to a tool result, and
    # appends a new prompt to the user turn holding the previous one: a tool result with
    # no prompt of the user's is the answer to a tool call.
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    prompts = [b.get("text", "") for b in blocks if b.get("type") == "text"
               and not b.get("text", "").lstrip().startswith("<system-reminder>")]  # fmt: skip
    result = _tool_result(last)
    if result is not None and not any(p.strip() for p in prompts):
        return "text", f"Готово: {result.strip()}"
    prompt = " ".join(prompts).lower()
    if "bash" in prompt:
        return "tool", ("Bash", {"command": "echo FAKE-$((6*7))", "description": "Print 42"})
    if "platform" in prompt:  # the platform's MCP tool (ADR-0038)
        return "tool", ("mcp__platform__info", {})
    if "plan" in prompt:
        return "tool", ("ExitPlanMode", {"plan": "1. Добавить hello.txt\n2. Проверить"})
    if "write" in prompt:
        name = "second.txt" if "another" in prompt else "hello.txt"
        return "tool", ("Write", {"file_path": f"/workspace/{name}", "content": "hello\n"})
    return "text", f"Эхо: {_text_of(last).strip()[-200:]}"


# Claude Code pairs tool results with calls, and merges messages, by id: unique ones.
_SERIAL = itertools.count(1)


def _events(kind: str, payload: Any, serial: int) -> list[tuple[str, dict[str, Any]]]:
    usage = {"input_tokens": 12, "output_tokens": 1, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0}  # fmt: skip
    events = [("message_start", {"type": "message_start", "message": {
        "id": f"msg_fake{serial}", "type": "message", "role": "assistant", "model": "claude-fake",
        "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}})]  # fmt: skip
    if kind == "text":
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": payload}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]  # fmt: skip
        stop = "end_turn"
    else:
        name, tool_input = payload
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use", "name": name,
                                                       "id": f"toolu_fake{serial}", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": json.dumps(tool_input)}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]  # fmt: skip
        stop = "tool_use"
    events += [
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop,
                                                               "stop_sequence": None},
                           "usage": {"output_tokens": 9}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return events


class FakeAnthropic:
    """``with FakeAnthropic() as fake: env ANTHROPIC_BASE_URL=fake.url``."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                if not self.path.split("?")[0].endswith("/v1/messages"):
                    self._json({"input_tokens": 12})  # count_tokens and the like
                    return
                kind, payload = plan(body)
                serial = next(_SERIAL)
                if not body.get("stream"):
                    content = ([{"type": "text", "text": payload}] if kind == "text" else
                               [{"type": "tool_use", "id": f"toolu_fake{serial}",
                                 "name": payload[0], "input": payload[1]}])  # fmt: skip
                    self._json({"id": f"msg_fake{serial}", "type": "message", "role": "assistant",
                                "model": "claude-fake", "content": content,
                                "stop_reason": "end_turn" if kind == "text" else "tool_use",
                                "usage": {"input_tokens": 12, "output_tokens": 9}})  # fmt: skip
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for event, data in _events(kind, payload, serial):
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()

            def do_GET(self) -> None:
                self._json({"data": []})

            def _json(self, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeAnthropic:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
