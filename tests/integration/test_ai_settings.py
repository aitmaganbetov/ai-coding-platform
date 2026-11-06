"""The orchestrator's AI provider configured from the Web UI (ADR-0046): the web settings
override the project's file for server runs; the key is a write-only project secret that
reaches the provider through the server's credentials, never an answer, an audit event,
a log line or ``os.environ``."""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_coding.agents.llm_factory import CREDENTIALS, build_llm, plan_chain
from ai_coding.api.ai_settings import key_env_name
from ai_coding.api.app import create_app
from ai_coding.api.core import InvalidError
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import AgentName, Role
from ai_coding.persistence.models import AuditEvent, Project

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
KEY = "sk-test-NEVER-SHOWN-1234567890abcdef"


class FakeProvider:
    """``GET /v1/models`` of OpenAI (Bearer) or Anthropic (x-api-key); a wrong key is
    echoed back in the error, as real providers partly do — the platform must hide it."""

    def __init__(self, kind: str, models: list[str]) -> None:
        outer = self
        self.seen: list[dict[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def do_GET(self) -> None:
                outer.seen.append(dict(self.headers))
                if self.path.rstrip("/") != "/v1/models":
                    self._reply(404, {"error": {"message": "not found"}})
                    return
                bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                given = self.headers.get("x-api-key") if kind == "anthropic" else bearer
                if given != KEY:
                    self._reply(401, {"error": {"message": f"Incorrect API key provided: {given}"}})
                    return
                self._reply(200, {"data": [{"id": m} for m in models]})

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    root = tmp_path / "repos" / "alpha"
    root.mkdir(parents=True)
    db = f"sqlite:///{tmp_path / 's.db'}"
    srv = Server(ServerSettings(data_dir=tmp_path / "data", database_url=db,
                                allowed_roots=(root.parent.resolve(),),
                                secret_key=base64.b64encode(os.urandom(32)).decode()))  # fmt: skip
    srv.migrate()
    for name in ("owner", "maint", "dev", "outsider"):
        srv.create_user(name, PASSWORD)
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    pid = srv.create_project(owner, "alpha", path=str(root))
    srv.set_member(owner, pid, "maint", Role.MAINTAINER)
    srv.set_member(owner, pid, "dev", Role.DEVELOPER)
    openai, anthropic = (
        FakeProvider("openai", ["gpt-fake", "gpt-other"]),
        FakeProvider("anthropic", ["claude-fake"]),
    )
    with TestClient(create_app(srv, broker=None)) as client:
        heads = {}
        for name in ("owner", "maint", "dev", "outsider"):
            creds = {"username": name, "password": PASSWORD}
            heads[name] = {
                "Authorization": "Bearer "
                + client.post("/api/v1/auth/login", json=creds).json()["token"]
            }
        yield {
            "c": client,
            "srv": srv,
            "pid": pid,
            "h": heads,
            "openai": openai,
            "anthropic": anthropic,
        }
    openai.close()
    anthropic.close()
    CREDENTIALS.unregister(key_env_name(pid))
    srv.close()


def effective_config(srv: Server, pid: uuid.UUID) -> Any:
    with srv.session() as s:
        project = s.get(Project, pid)
        assert project is not None
        s.expunge(project)
    return srv.project_config(project)[1]


def test_configure_check_and_use_a_provider(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    c, srv, pid, h, fake = world["c"], world["srv"], world["pid"], world["h"], world["openai"]
    url = f"/api/v1/projects/{pid}/ai"
    seen = c.get(url, headers=h["dev"]).json()
    assert seen["can_edit"] is False and seen["effective"]["enabled"] is False
    assert seen["sources"]["provider"] == "file" and seen["key"]["set"] is False
    assert "developer" in seen["agents"]

    settings = {"enabled": True, "provider": "openai", "model": "gpt-fake",
                "base_url": f"{fake.url}/v1",
                "private": False, "routing": {"reviewer": "gpt-other"}}  # fmt: skip
    assert c.put(url, json={"settings": settings}, headers=h["dev"]).status_code == 403
    saved = c.put(url, json={"settings": settings, "version": 0}, headers=h["maint"])
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert (
        view["valid"]
        and view["effective"]["provider"] == "openai"
        and view["sources"]["provider"] == "web"
    )
    assert view["effective"]["routing"] == {"reviewer": "gpt-other"} and view["version"] == 1
    stale = c.put(url, json={"settings": settings, "version": 0}, headers=h["maint"])
    assert stale.status_code == 409

    # No key yet: the provider refuses, the answer says so.
    probe = c.post(f"{url}/test", headers=h["maint"]).json()
    assert probe["ok"] is False and "401" in probe["detail"]
    assert (
        c.put(
            f"/api/v1/projects/{pid}/secrets/ai.api_key", json={"value": KEY}, headers=h["maint"]
        ).status_code
        == 200
    )
    probe = c.post(f"{url}/test", headers=h["maint"])
    assert probe.json()["ok"] is True and probe.json()["models_offered"] == 2, probe.text
    assert fake.seen[-1]["Authorization"] == f"Bearer {KEY}"  # the key reached the provider
    models = c.get(f"{url}/models", headers=h["maint"]).json()
    assert models == {"provider": "openai", "models": ["gpt-fake", "gpt-other"], "reason": None}
    assert c.get(url, headers=h["dev"]).json()["key"] == {"set": True, "source": "web", "env": None}

    # A run's LLM gets the key from the server's credentials, not from os.environ.
    config = effective_config(srv, pid)
    assert config.ai.api_key_env == key_env_name(pid) and key_env_name(pid) not in os.environ
    assert [r.model for r in plan_chain(config, AgentName.REVIEWER, env={}).routes] == ["gpt-other"]
    llm = build_llm(config, AgentName.DEVELOPER, policy=None, budget=None, env={})  # type: ignore[arg-type]
    assert getattr(llm.primary.provider, "_api_key", None) == KEY

    # A wrong key: the provider echoes it, the platform does not.
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    wrong = "".join(("sk-", "wrong-ECHOED-0987654321"))  # built here: no key shape in git
    srv.set_secret(owner, pid, "ai.api_key", wrong)
    probe = c.post(f"{url}/test", headers=h["maint"]).json()
    assert probe["ok"] is False and wrong not in json.dumps(probe)

    everything = json.dumps([c.get(url, headers=h["owner"]).json(), probe, models])
    with srv.session() as s:
        events = [
            (e.action, e.target, json.dumps(e.details))
            for e in s.query(AuditEvent).filter(AuditEvent.project_id == pid)
        ]
    assert {"project.settings", "project.ai.test", "project.secret.set"} <= {
        a for a, _, _ in events
    }
    logs = capsys.readouterr()
    for text in (everything, json.dumps(events), logs.out, logs.err):
        assert KEY not in text and wrong not in text


def test_anthropic_and_access(world: dict[str, Any]) -> None:
    c, pid, h, fake = world["c"], world["pid"], world["h"], world["anthropic"]
    url = f"/api/v1/projects/{pid}/ai"
    assert c.get(url, headers=h["outsider"]).status_code in (403, 404)
    assert c.post(f"{url}/test", headers=h["outsider"]).status_code in (403, 404)
    assert c.post(f"{url}/test", headers=h["dev"]).status_code == 403
    settings = {
        "enabled": True,
        "provider": "anthropic",
        "model": "claude-fake",
        "base_url": fake.url,
        "private": False,
    }
    assert c.put(url, json={"settings": settings}, headers=h["owner"]).status_code == 200
    c.put(f"/api/v1/projects/{pid}/secrets/ai.api_key", json={"value": KEY}, headers=h["owner"])
    probe = c.post(f"{url}/test", headers=h["owner"]).json()
    assert probe["ok"] is True and probe["provider"] == "anthropic", probe
    assert fake.seen[-1]["x-api-key"] == KEY and fake.seen[-1]["anthropic-version"]
    # A model the provider does not offer.
    c.put(url, json={"settings": {**settings, "model": "claude-missing"}}, headers=h["owner"])
    assert "not offered" in c.post(f"{url}/test", headers=h["owner"]).json()["detail"]


def test_private_mode_keeps_the_code_local(world: dict[str, Any]) -> None:
    c, pid, h = world["c"], world["pid"], world["h"]
    url = f"/api/v1/projects/{pid}/ai"
    external = {"enabled": True, "provider": "openai", "model": "gpt-4o", "private": True}
    refused = c.put(url, json={"settings": external}, headers=h["owner"])
    assert refused.status_code == 422 and "local-only" in refused.text
    local = {
        "enabled": True,
        "provider": "ollama",
        "model": "llama3",
        "base_url": "http://localhost:11434/v1",
        "private": True,
    }
    view = c.put(url, json={"settings": local}, headers=h["owner"]).json()
    assert view["effective"]["private"] is True and view["effective"]["network"] == "local-only"
    # Private Mode off: only the provider's host is let through.
    view = c.put(url, json={"settings": {**external, "private": False}}, headers=h["owner"]).json()
    assert view["effective"]["network"] == "allowlist"
    assert effective_config(world["srv"], pid).security.egress_allowlist == ["api.openai.com"]
    # A remote endpoint over plain http is refused; cloud metadata is never called.
    bad = c.put(
        url,
        json={"settings": {**external, "private": False, "base_url": "http://example.com/v1"}},
        headers=h["owner"],
    )
    assert bad.status_code == 422 and "https" in bad.text
    meta = {
        "enabled": True,
        "provider": "vllm",
        "model": "m",
        "base_url": "http://169.254.169.254/v1",
        "private": False,
    }
    assert c.put(url, json={"settings": meta}, headers=h["owner"]).status_code == 200
    probe = c.post(f"{url}/test", headers=h["owner"]).json()
    assert probe["ok"] is False and "cannot be called" in probe["detail"]


def test_the_files_key_variable_stays_for_the_same_provider(world: dict[str, Any]) -> None:
    c, srv, pid, h = world["c"], world["srv"], world["pid"], world["h"]
    with srv.session() as s:
        project = s.get(Project, pid)
        assert project is not None
        assert project.root_path is not None
        root = Path(project.root_path)
    (root / ".ai-coding.yml").write_text(
        "schema_version: '2.1'\nproject: {name: alpha}\n"
        "ai: {enabled: true, provider: openai, model: gpt-file, api_key_env: OPENAI_KEY_OF_FILE}\n"
        "security: {network: allowlist, egress_allowlist: [api.openai.com]}\n"
    )
    url = f"/api/v1/projects/{pid}/ai"
    # The web changes the model only: the file's variable still holds the key.
    view = c.put(
        url, json={"settings": {"provider": "openai", "model": "gpt-web"}}, headers=h["owner"]
    ).json()
    assert view["key"] == {"set": False, "source": "env", "env": "OPENAI_KEY_OF_FILE"}
    assert view["sources"]["model"] == "web" and view["file"]["model"] == "gpt-file"
    assert effective_config(srv, pid).ai.api_key_env == "OPENAI_KEY_OF_FILE"
    # Another provider: the file's variable was for the old one.
    view = c.put(
        url, json={"settings": {"provider": "anthropic", "model": "claude-web"}}, headers=h["owner"]
    ).json()
    assert view["key"] == {"set": False, "source": "web", "env": None}
    # A key set in the web wins over the file's variable.
    c.put(url, json={"settings": {"provider": "openai"}}, headers=h["owner"])
    c.put(f"/api/v1/projects/{pid}/secrets/ai.api_key", json={"value": KEY}, headers=h["owner"])
    assert c.get(url, headers=h["owner"]).json()["key"] == {
        "set": True,
        "source": "web",
        "env": None,
    }
    assert effective_config(srv, pid).ai.api_key_env == key_env_name(pid)


def test_a_project_cannot_name_the_platforms_variables(world: dict[str, Any]) -> None:
    """The credentials are shared by the process and resolved by name: another project's
    file must not reach the web key of this one, nor the server's own secrets."""
    c, srv, pid, h, fake = world["c"], world["srv"], world["pid"], world["h"], world["openai"]
    url = f"/api/v1/projects/{pid}/ai"
    settings = {"enabled": True, "provider": "openai", "model": "gpt-fake",
                "base_url": f"{fake.url}/v1", "private": False}  # fmt: skip
    c.put(url, json={"settings": settings}, headers=h["owner"])
    c.put(f"/api/v1/projects/{pid}/secrets/ai.api_key", json={"value": KEY}, headers=h["owner"])
    assert effective_config(srv, pid).ai.api_key_env == key_env_name(pid)  # registered

    # Anyone may create a project: its file names alpha's web key, then the server's key.
    root = Path(str(srv.settings.allowed_roots[0])) / "beta"
    root.mkdir()
    outsider = srv.login("outsider", PASSWORD)
    assert outsider is not None
    beta = srv.create_project(outsider, "beta", path=str(root))
    for stolen in (key_env_name(pid), "AI_CODING_SERVER_SECRET_KEY"):
        (root / ".ai-coding.yml").write_text(
            "schema_version: '2.1'\nproject: {name: beta}\n"
            f"ai: {{enabled: true, provider: openai, model: gpt-fake, base_url: '{fake.url}/v1',"
            f" api_key_env: {stolen}}}\n"
            "security: {network: open}\n"
        )
        fake.seen.clear()
        with pytest.raises(InvalidError, match="belong to the platform"):
            effective_config(srv, beta)
        probe = c.post(f"/api/v1/projects/{beta}/ai/test", headers=h["outsider"])
        assert probe.status_code == 422 and "belong to the platform" in probe.text
        view = c.get(f"/api/v1/projects/{beta}/ai", headers=h["outsider"]).json()
        assert view["valid"] is False and view["key"] == {
            "set": False,
            "source": "env",
            "env": stolen,
        }
        assert fake.seen == []  # nothing was sent anywhere
    # The project's own web key stays usable.
    assert effective_config(srv, pid).ai.api_key_env == key_env_name(pid)
