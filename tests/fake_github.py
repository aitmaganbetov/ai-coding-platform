"""In-memory fake of the GitHub REST API used by the platform. Test-only double.

Serves real HTTP on localhost so the production client, CLI and workflows are
exercised end-to-end (headers, pagination, status codes) without the network.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class FakeGitHub:
    repo: str = "acme/demo"
    token: str = "test-token-0123456789"
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    comments: dict[int, dict[str, Any]] = field(default_factory=dict)
    check_runs: dict[int, dict[str, Any]] = field(default_factory=dict)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    rules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    runs: dict[int, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    head_shas: dict[str, str] = field(default_factory=dict)  # branch -> sha (set by tests)
    artifacts: dict[int, list[dict[str, Any]]] = field(default_factory=dict)  # run id -> list
    blobs: dict[int, bytes] = field(default_factory=dict)  # artifact id -> zip bytes
    artifacts_on_dispatch: list[dict[str, Any]] = field(default_factory=list)
    blob_requests: list[str | None] = field(default_factory=list)  # Authorization seen
    _next: int = 100
    _lock: threading.Lock = field(default_factory=threading.Lock)
    server: ThreadingHTTPServer | None = None

    def next_id(self) -> int:
        self._next += 1
        return self._next

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://localhost:{self.server.server_address[1]}"

    def start(self) -> FakeGitHub:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send(
                self, status: int, body: Any = None, headers: dict[str, str] | None = None
            ) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null")

            def _blob(self, artifact_id: int) -> None:
                # Signed blob storage: a different "host" that must not get the token.
                fake.blob_requests.append(self.headers.get("Authorization"))
                data = fake.blobs.get(artifact_id, b"")
                self.send_response(200 if artifact_id in fake.blobs else 404)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self, method: str) -> None:
                blob = re.fullmatch(r"/blob/(\d+)", urlsplit(self.path).path)
                if blob and method == "GET":
                    self._blob(int(blob.group(1)))
                    return
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send(401, {"message": "Bad credentials"})
                    return
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                fake.requests.append((method, parts.path))
                with fake._lock:
                    status, body = fake.handle(
                        method,
                        parts.path,
                        query,
                        self._body() if method in {"POST", "PATCH"} else None,
                    )
                if status == 302:
                    self._send(302, None, {"Location": body["location"]})
                    return
                self._send(status, body)

            def do_GET(self) -> None:
                self._route("GET")

            def do_POST(self) -> None:
                self._route("POST")

            def do_PATCH(self) -> None:
                self._route("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    # ---------------------------------------------------------------- routes --
    def handle(self, method: str, path: str, q: dict[str, str], body: Any) -> tuple[int, Any]:  # noqa: PLR0911, PLR0912, PLR0915
        base = f"/repos/{self.repo}"
        if not path.startswith(base) and path != base:
            return 404, {"message": "Not Found"}
        rest = path[len(base) :]
        if rest == "" and method == "GET":
            return 200, {"full_name": self.repo}
        if rest == "/pulls" and method == "GET":
            head = q.get("head", "").split(":", 1)[-1]
            items = [
                p
                for p in self.pulls.values()
                if p["state"] == q.get("state", "open")
                and (not head or p["head"]["ref"] == head)
                and (not q.get("base") or p["base"]["ref"] == q["base"])
            ]
            return 200, items
        if rest == "/pulls" and method == "POST":
            number = len(self.pulls) + 1
            head = body["head"]
            self.pulls[number] = {
                "number": number,
                "html_url": f"https://github.com/{self.repo}/pull/{number}",
                "head": {"ref": head, "sha": self.head_shas.get(head, "0" * 40)},
                "base": {"ref": body["base"]},
                "state": "open",
                "draft": body.get("draft", False),
                "title": body["title"],
                "body": body["body"],
                "auto_merge": None,
                "maintainer_can_modify": body.get("maintainer_can_modify"),
            }
            return 201, self.pulls[number]
        m = re.fullmatch(r"/pulls/(\d+)", rest)
        if m and method == "GET":
            pr = self.pulls.get(int(m.group(1)))
            return (200, pr) if pr else (404, {"message": "Not Found"})
        m = re.fullmatch(r"/issues/(\d+)/comments", rest)
        if m and method == "GET":
            return 200, [c for c in self.comments.values() if c["issue"] == int(m.group(1))]
        if m and method == "POST":
            cid = self.next_id()
            self.comments[cid] = {"id": cid, "issue": int(m.group(1)), "body": body["body"]}
            return 201, self.comments[cid]
        m = re.fullmatch(r"/issues/comments/(\d+)", rest)
        if m and method == "PATCH":
            self.comments[int(m.group(1))]["body"] = body["body"]
            return 200, self.comments[int(m.group(1))]
        if rest == "/check-runs" and method == "POST":
            cid = self.next_id()
            self.check_runs[cid] = {
                **body,
                "id": cid,
                "annotation_batches": [len(body["output"]["annotations"])],
            }
            return 201, self.check_runs[cid]
        m = re.fullmatch(r"/check-runs/(\d+)", rest)
        if m and method == "PATCH":
            self.check_runs[int(m.group(1))]["annotation_batches"].append(
                len(body["output"]["annotations"])
            )
            return 200, self.check_runs[int(m.group(1))]
        m = re.fullmatch(r"/statuses/([0-9a-f]{40})", rest)
        if m and method == "POST":
            self.statuses.append({**body, "sha": m.group(1)})
            return 201, body
        m = re.fullmatch(r"/rules/branches/(.+)", rest)
        if m and method == "GET":
            branch = m.group(1)
            return (200, self.rules[branch]) if branch in self.rules else (200, [])
        m = re.fullmatch(r"/actions/workflows/([^/]+)/dispatches", rest)
        if m and method == "POST":
            run_id = self.next_id()
            corr = body["inputs"].get("correlation_id", "")
            self.dispatches.append({"workflow": m.group(1), **body})
            self.runs[run_id] = {
                "id": run_id,
                "display_title": f"ai-coding {corr}",
                "status": "queued",
                "conclusion": None,
                "html_url": f"https://github.com/{self.repo}/actions/runs/{run_id}",
                "workflow": m.group(1),
                "polls": 0,
            }
            self.jobs[run_id] = [{"name": "test", "conclusion": None}]
            self.artifacts[run_id] = list(self.artifacts_on_dispatch)
            return 204, None
        m = re.fullmatch(r"/actions/runs/(\d+)/artifacts", rest)
        if m and method == "GET":
            arts = self.artifacts.get(int(m.group(1)), [])
            return 200, {"total_count": len(arts), "artifacts": arts}
        m = re.fullmatch(r"/actions/artifacts/(\d+)/zip", rest)
        if m and method == "GET":
            return 302, {"location": f"{self.url}/blob/{m.group(1)}"}
        m = re.fullmatch(r"/actions/workflows/([^/]+)/runs", rest)
        if m and method == "GET":
            runs = [r for r in self.runs.values() if r["workflow"] == m.group(1)]
            return 200, {"total_count": len(runs), "workflow_runs": runs}
        m = re.fullmatch(r"/actions/runs/(\d+)", rest)
        if m and method == "GET":
            run = self.runs[int(m.group(1))]
            run["polls"] += 1
            if run["polls"] >= 2 and run["status"] != "completed" and not run.get("hang"):
                run["status"] = "completed"
                run["conclusion"] = run.get("final", "success")
                for job in self.jobs[run["id"]]:
                    job["conclusion"] = run["conclusion"]
            return 200, run
        m = re.fullmatch(r"/actions/runs/(\d+)/cancel", rest)
        if m and method == "POST":
            self.runs[int(m.group(1))]["status"] = "completed"
            self.runs[int(m.group(1))]["conclusion"] = "cancelled"
            return 202, {}
        m = re.fullmatch(r"/actions/runs/(\d+)/jobs", rest)
        if m and method == "GET":
            return 200, {"total_count": 1, "jobs": self.jobs[int(m.group(1))]}
        return 404, {"message": f"no fake route for {method} {path}"}
