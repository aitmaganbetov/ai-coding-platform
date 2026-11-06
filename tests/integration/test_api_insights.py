"""Read-only views for the Web UI: agent catalogue, runs and findings across projects,
agent executions, LLM usage, run details, configuration summary, system status."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.config.loader import STATE_CONFIG
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import (
    AgentExecution,
    Finding,
    ModelUsage,
    RunJob,
    Task,
)
from ai_coding.persistence.types import utcnow
from ai_coding.tools.cache import ENV as CACHE_ENV

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
GATES = [
    {"gate": "security", "result": "FAILED", "blocking": True, "reason": "1 high finding",
     "evidence": ["semgrep:abc"]},
    {"gate": "tests", "result": "SKIPPED", "blocking": True, "reason": "no tests",
     "evidence": []},
]  # fmt: skip


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """alpha and beta of ``dev`` (``aud`` audits alpha), gamma of ``outsider``; one
    discover run each, findings, LLM usage and a gates output on alpha's run."""
    tmp = tmp_path_factory.mktemp("insights")
    roots = tmp / "repos"
    for name in ("alpha", "beta", "gamma"):
        (roots / name).mkdir(parents=True)
        (roots / name / "main.py").write_text("print(1)\n", encoding="utf-8")
    srv = Server(ServerSettings(data_dir=tmp / "data", database_url=f"sqlite:///{tmp / 's.db'}",
                                allowed_roots=(roots.resolve(),),
                                rate_limit_per_minute=100_000))  # fmt: skip
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    for name in ("dev", "aud", "outsider"):
        srv.create_user(name, PASSWORD)
    dev, outsider = srv.login("dev", PASSWORD), srv.login("outsider", PASSWORD)
    assert dev is not None and outsider is not None
    ids: dict[str, Any] = {"server": srv}
    for name, owner in (("alpha", dev), ("beta", dev), ("gamma", outsider)):
        ids[name] = srv.create_project(owner, name, path=str(roots.resolve() / name))
        ids[f"{name}_run"] = srv.create_run(owner, ids[name], "discover", {})
    srv.set_member(dev, ids["alpha"], "aud", Role.AUDITOR)
    asyncio.run(Worker(srv, poll_seconds=0.01).run_until_idle(timeout=120))
    now = utcnow()
    with srv.session() as s:
        report = s.scalar(select(Task).where(Task.run_id == ids["alpha_run"],
                                             Task.key == "report.discovery"))  # fmt: skip
        assert report is not None
        ids["report_task"] = report.id
        s.add(Task(run_id=ids["alpha_run"], key="quality.gates", name="Quality gates",
                   phase="verification", capability="quality.gates", status="SUCCEEDED",
                   output={"verdict": "FAILED", "gates": GATES}))  # fmt: skip
        for model, provider, tokens_in, tokens_out, cost, age in (
            ("claude-x", "anthropic", 100, 50, 0.01, timedelta(0)),
            ("claude-x", "anthropic", 20, 10, 0.002, timedelta(days=2)),
            ("gpt-y", "openai", 7, 3, 0.0005, timedelta(days=40)),
        ):
            s.add(ModelUsage(run_id=ids["alpha_run"], task_id=report.id, provider=provider,
                             model=model, input_tokens=tokens_in, output_tokens=tokens_out,
                             cost_usd=cost, created_at=now - age))  # fmt: skip
        s.add(AgentExecution(task_id=report.id, agent_name="agent.reviewer", attempt=9,
                             status="RUNNING", error="x" * 600,
                             started_at=now + timedelta(seconds=5)))  # fmt: skip
        for project, severity, source, status in (
            ("alpha", "high", "semgrep", "open"),
            ("alpha", "low", "ruff", "suppressed"),
            ("beta", "high", "gitleaks", "open"),
            ("gamma", "high", "semgrep", "open"),
        ):
            s.add(Finding(project_id=ids[project], run_id=ids[f"{project}_run"], source=source,
                          severity=severity, title=f"{project} {source}", status=status,
                          fingerprint=uuid.uuid4().hex))  # fmt: skip
    with TestClient(create_app(srv)) as client:
        ids["client"] = client
        for name in ("admin", "dev", "aud", "outsider"):
            r = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
            ids[name] = {"Authorization": f"Bearer {r.json()['token']}"}
        yield ids
    srv.close()


def _get(world: dict[str, Any], who: str, path: str, **params: Any) -> Any:
    r = world["client"].get(f"/api/v1{path}", headers=world[who], params=params)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def _status(world: dict[str, Any], who: str, path: str, **params: Any) -> int:
    headers = world[who] if who else {}
    code: int = world["client"].get(f"/api/v1{path}", headers=headers, params=params).status_code
    return code


def test_agent_catalogue(world: dict[str, Any]) -> None:
    agents = _get(world, "aud", "/agents")
    assert len(agents) == 12
    keys = {"name", "inputs", "outputs", "requires_llm", "implemented", "sprint",
            "capabilities", "allowed_tools"}  # fmt: skip
    assert all(set(a) == keys for a in agents)
    developer = next(a for a in agents if a["name"] == "developer")
    assert developer["capabilities"] == ["agent.developer"] and developer["allowed_tools"] == []
    assert developer["requires_llm"] is True and developer["implemented"] is True
    assert _status(world, "", "/agents") == 401


def test_runs_of_every_visible_project(world: dict[str, Any]) -> None:
    runs = _get(world, "dev", "/runs")
    assert {r["project_name"] for r in runs} == {"alpha", "beta"}
    assert set(runs[0]) == {"id", "project_id", "project_name", "state", "mode", "kind",
                            "goal", "requested_by", "created_at", "finished_at"}  # fmt: skip
    assert [r["created_at"] for r in runs] == sorted((r["created_at"] for r in runs),
                                                     reverse=True)  # fmt: skip
    assert runs[0]["id"] == str(world["beta_run"]) and runs[0]["kind"] == "discover"
    assert runs[0]["requested_by"] == "dev" and runs[0]["finished_at"] is not None
    # The auditor sees alpha only, the outsider gamma only, an admin who is not a member
    # nothing (administrators get no implicit access to project data).
    assert [r["project_name"] for r in _get(world, "aud", "/runs")] == ["alpha"]
    assert [r["project_name"] for r in _get(world, "outsider", "/runs")] == ["gamma"]
    assert _get(world, "admin", "/runs") == []
    assert _status(world, "", "/runs") == 401


def test_run_filters(world: dict[str, Any]) -> None:
    state = _get(world, "dev", "/runs")[0]["state"]
    alpha, gamma = str(world["alpha"]), str(world["gamma"])
    assert len(_get(world, "dev", "/runs", state=state, kind="discover")) == 2
    assert len(_get(world, "dev", "/runs", state="", kind="", project_id="")) == 2
    assert _get(world, "dev", "/runs", kind="audit") == []
    assert _get(world, "dev", "/runs", state="NO_SUCH_STATE") == []
    assert [r["project_id"] for r in _get(world, "dev", "/runs", project_id=alpha)] == [alpha]
    assert _get(world, "dev", "/runs", project_id=gamma) == []  # not theirs: no hint
    assert _get(world, "dev", "/runs", project_id="not-a-uuid") == []
    assert len(_get(world, "dev", "/runs", limit=1)) == 1
    assert len(_get(world, "dev", "/runs", limit=100_000)) == 2  # capped, not refused
    assert _status(world, "dev", "/runs", limit=0) == 422


def test_a_project_token_sees_its_project_only(world: dict[str, Any]) -> None:
    client = world["client"]
    body = {"name": "alpha-ci", "project_id": str(world["alpha"])}
    token = client.post("/api/v1/tokens", headers=world["dev"], json=body).json()["token"]
    scoped = {"Authorization": f"Bearer {token}"}
    runs = client.get("/api/v1/runs", headers=scoped).json()
    assert [r["project_name"] for r in runs] == ["alpha"]
    findings = client.get("/api/v1/findings", headers=scoped).json()
    assert {f["project_name"] for f in findings} == {"alpha"}
    usage = client.get(f"/api/v1/projects/{world['beta']}/usage", headers=scoped)
    assert usage.status_code == 404


def test_findings_of_every_visible_project(world: dict[str, Any]) -> None:
    findings = _get(world, "dev", "/findings")
    assert sorted(f["title"] for f in findings) == ["alpha ruff", "alpha semgrep",
                                                    "beta gitleaks"]  # fmt: skip
    first = findings[0]
    assert {"id", "project_id", "run_id", "source", "severity", "title", "status",
            "fingerprint", "created_at", "project_name"} <= set(first)  # fmt: skip
    assert [f["created_at"] for f in findings] == sorted((f["created_at"] for f in findings),
                                                         reverse=True)  # fmt: skip
    assert {f["project_name"] for f in _get(world, "aud", "/findings")} == {"alpha"}
    assert [f["title"] for f in _get(world, "outsider", "/findings")] == ["gamma semgrep"]

    def titles(**query: str) -> list[str]:
        return sorted(f["title"] for f in _get(world, "dev", "/findings", **query))

    assert titles(severity="high") == ["alpha semgrep", "beta gitleaks"]
    assert titles(status="suppressed") == ["alpha ruff"]
    assert titles(source="gitleaks") == ["beta gitleaks"]
    assert titles(project_id=str(world["alpha"]), severity="high") == ["alpha semgrep"]
    assert titles(run_id=str(world["beta_run"])) == ["beta gitleaks"]
    assert titles(run_id=str(world["gamma_run"])) == []
    assert titles(project_id=str(world["gamma"])) == []
    assert titles(severity="nonsense", run_id="garbage") == []
    assert len(_get(world, "dev", "/findings", limit=1)) == 1
    assert _status(world, "", "/findings") == 401


def test_agent_executions(world: dict[str, Any]) -> None:
    alpha = world["alpha"]
    rows = _get(world, "dev", f"/projects/{alpha}/agent-executions")
    assert set(rows[0]) == {"id", "run_id", "task_key", "agent_name", "attempt", "status",
                            "confidence", "error", "started_at", "finished_at",
                            "duration_seconds", "input_tokens", "output_tokens", "cost_usd",
                            "models"}  # fmt: skip
    assert {r["run_id"] for r in rows} == {str(world["alpha_run"])}  # alpha's runs only
    # The newest: the attempt still running, its error clipped.
    running = rows[0]
    assert running["agent_name"] == "agent.reviewer" and running["finished_at"] is None
    assert running["duration_seconds"] is None and len(running["error"]) == 500
    # Usage of the task's LLM calls (every call of the task, any age).
    assert running["input_tokens"] == 127 and running["output_tokens"] == 63
    assert running["cost_usd"] == pytest.approx(0.0125)
    assert running["models"] == ["anthropic/claude-x", "openai/gpt-y"]
    finished = [r for r in rows if r["finished_at"] is not None]
    assert {r["task_key"] for r in finished} >= {"discover.config", "report.discovery"}
    assert all(r["duration_seconds"] >= 0 for r in finished)
    probe = next(r for r in finished if r["task_key"] == "discover.git")
    assert probe["input_tokens"] == 0 and probe["cost_usd"] == 0 and probe["models"] == []
    assert len(_get(world, "dev", f"/projects/{alpha}/agent-executions", limit=2)) == 2
    assert len(_get(world, "aud", f"/projects/{alpha}/agent-executions")) == len(rows)
    refused = world["client"].get(f"/api/v1/projects/{alpha}/agent-executions",
                                  headers=world["outsider"])  # fmt: skip
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "not_found"
    assert _status(world, "admin", f"/projects/{alpha}/agent-executions") == 404


def test_usage(world: dict[str, Any]) -> None:
    alpha = world["alpha"]
    month = _get(world, "aud", f"/projects/{alpha}/usage")
    assert month["totals"] == {"calls": 2, "input_tokens": 120, "output_tokens": 60,
                               "cost_usd": pytest.approx(0.012)}  # fmt: skip
    assert month["by_model"] == [{"provider": "anthropic", "model": "claude-x", "calls": 2,
                                  "input_tokens": 120, "output_tokens": 60,
                                  "cost_usd": pytest.approx(0.012)}]  # fmt: skip
    today, two_days_ago = utcnow().date(), (utcnow() - timedelta(days=2)).date()
    assert [d["date"] for d in month["daily"]] == [two_days_ago.isoformat(), today.isoformat()]
    assert month["daily"][1] == {"date": today.isoformat(), "input_tokens": 100,
                                 "output_tokens": 50, "cost_usd": pytest.approx(0.01)}  # fmt: skip
    year = _get(world, "dev", f"/projects/{alpha}/usage", days=365)
    assert year["totals"]["calls"] == 3 and len(year["by_model"]) == 2 and len(year["daily"]) == 3
    empty = _get(world, "dev", f"/projects/{world['beta']}/usage", days=1)
    assert empty == {"totals": {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                "cost_usd": 0.0}, "by_model": [], "daily": []}  # fmt: skip
    for days in (0, 366):
        assert _status(world, "dev", f"/projects/{alpha}/usage", days=days) == 422
    assert _status(world, "outsider", f"/projects/{alpha}/usage") == 404


def test_run_detail_has_graph_gates_and_usage(world: dict[str, Any]) -> None:
    run = _get(world, "aud", f"/runs/{world['alpha_run']}")
    assert {"id", "state", "tasks", "approvals", "summary", "project_id", "kind",
            "jobs"} <= set(run)  # fmt: skip
    assert run["dependencies"] == [
        {"task": "report.discovery", "depends_on": f"discover.{probe}"}
        for probe in ("config", "context", "git", "stacks")
    ]
    assert run["gates"] == GATES
    assert run["usage"] == {"input_tokens": 127, "output_tokens": 63,
                            "cost_usd": pytest.approx(0.0125)}  # fmt: skip
    other = _get(world, "dev", f"/runs/{world['beta_run']}")
    assert other["gates"] is None and len(other["dependencies"]) == 4
    assert other["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def test_project_config_summary(world: dict[str, Any]) -> None:
    root = world["server"].settings.allowed_roots[0] / "beta"
    path = root / STATE_CONFIG
    original = path.read_text(encoding="utf-8")
    config = yaml.safe_load(original)
    # Inline credentials are refused by the validator; what could still leak is the key
    # variable's name, the endpoint and free-text commands.
    config["ai"] = {"enabled": True, "provider": "anthropic", "model": "claude-x",
                    "api_key_env": "SECRET_KEY_VARIABLE",
                    "base_url": "https://llm.internal.example.com/v1"}  # fmt: skip
    config["security"] = {"network": "allowlist", "egress_allowlist": ["llm.internal.example.com"]}
    config.setdefault("quality_gates", {})["tests"] = {
        "command": "pytest -q --private-flag",
        "install": "pip install --private-index x",
        "timeout_seconds": 600,
    }
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    try:
        r = world["client"].get(f"/api/v1/projects/{world['beta']}", headers=world["dev"])
        assert r.status_code == 200, r.text
        summary = r.json()["config"]
        assert {"members", "kinds", "role"} <= set(r.json())  # everything it had before
        assert set(summary) == {"stacks", "mode", "execution", "ai", "security", "budgets",
                                "quality_gates"}  # fmt: skip
        assert summary["ai"] == {"enabled": True, "provider": "anthropic", "model": "claude-x"}
        assert set(summary["execution"]) == {"backend", "network"}
        assert set(summary["security"]) == {"network", "egress_allowlist"}
        assert set(summary["budgets"]) == {"max_tokens_per_run", "max_cost_usd_per_run",
                                           "max_duration_minutes"}  # fmt: skip
        gates = summary["quality_gates"]
        assert gates["tests"]["timeout_seconds"] == 600 and gates["tests"]["enabled"] is True
        assert gates["coverage"]["min_percent"] == 80.0 and "blocking" in gates["build"]
        assert gates["security"]["fail_on"] == "high"
        assert summary["security"] == config["security"]
        text = json.dumps(summary)
        for secret in ("SECRET_KEY_VARIABLE", "/v1", "--private", "api_key_env", "base_url",
                       "command", "install"):  # fmt: skip
            assert secret not in text, secret
        # A broken configuration hides nothing but the summary.
        path.write_text("schema_version: [unclosed\n", encoding="utf-8")
        broken = world["client"].get(f"/api/v1/projects/{world['beta']}", headers=world["dev"])
        assert broken.status_code == 200 and broken.json()["config"] is None
        assert broken.json()["name"] == "beta"
    finally:
        path.write_text(original, encoding="utf-8")
    restored = _get(world, "dev", f"/projects/{world['beta']}")["config"]
    assert restored is not None and restored["ai"]["enabled"] is False


def test_system_status_for_admins_only(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "cache"))
    (tmp_path / "cache" / "trivy" / "db").mkdir(parents=True)
    (tmp_path / "cache" / "trivy" / "db" / "trivy.db").write_bytes(b"db")
    now, run_id = utcnow(), world["alpha_run"]
    with world["server"].session() as s:
        for owner, expires in (("host:1:live", now + timedelta(seconds=60)),
                               ("host:1:live", now + timedelta(seconds=30)),
                               ("host:2:stalled", now - timedelta(minutes=5)),
                               ("host:3:gone", now - timedelta(minutes=30))):  # fmt: skip
            s.add(RunJob(run_id=run_id, kind="resume", state="leased", lease_owner=owner,
                         lease_expires_at=expires, available_at=now))  # fmt: skip
    try:
        status = _get(world, "admin", "/system")
        assert set(status) == {"version", "database", "workspaces", "queue", "runs", "workers",
                               "analysers", "time"}  # fmt: skip
        assert status["database"] == "sqlite" and status["workspaces"] == "on"
        assert status["queue"]["leased"] == 4 and status["queue"]["done"] >= 3
        assert sum(status["runs"].values()) == 3
        assert [w["id"] for w in status["workers"]] == ["host:1:live", "host:2:stalled"]
        live, stalled = status["workers"]
        assert live["active_jobs"] == 2 and stalled["active_jobs"] == 0
        assert live["last_seen"] <= status["time"] and stalled["last_seen"] < status["time"]
        assert status["analysers"] == {"trivy": True, "osv-scanner": False}
        for who in ("dev", "aud", "outsider"):
            r = world["client"].get("/api/v1/system", headers=world[who])
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        assert _status(world, "", "/system") == 401
    finally:
        with world["server"].session() as s:
            for job in s.scalars(select(RunJob).where(RunJob.state == "leased")):
                s.delete(job)
    with TestClient(create_app(world["server"], broker=None)) as client:
        off = client.get("/api/v1/system", headers=world["admin"]).json()
        assert off["workspaces"] == "off"
