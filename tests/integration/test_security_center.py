"""AI Security Center API against a real database: opt-in switch, RBAC and token limits,
project isolation, filters and pagination, and no secret material in any response."""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.services import NotFoundError, Server
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import Role, Severity
from ai_coding.persistence.models import Finding, Run, Task

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
SECRET = "ghp_" + "a" * 36  # assembled at run time, never a real credential
CODE = "password = '" + "hunter2" * 3 + "'"
TOOL_ERROR = "exit code 2: token ghp_" + "b" * 36 + " in /home/alice/private/.env"
SKIP_REASON = "offline database missing in /home/alice/cache (run `ai-coding tools pull`)"
GATE_REASON = "gitleaks: " + SECRET
USERS = ("owner", "maintainer", "developer", "auditor", "outsider", "admin")
ROLES = {"maintainer": Role.MAINTAINER, "developer": Role.DEVELOPER, "auditor": Role.AUDITOR}


def _finding(project: uuid.UUID, run: uuid.UUID | None, n: int, **over: Any) -> Finding:
    data: dict[str, Any] = {
        "project_id": project,
        "run_id": run,
        "source": "semgrep",
        "rule_id": f"rule-{n}",
        "severity": Severity.LOW.value,
        "title": f"finding {n}",
        "message": SECRET,
        "file_path": f"src/file{n}.py",
        "line": n,
        "fingerprint": f"{n:064d}",
        "evidence": {},
    }
    return Finding(**{**data, **over})


def _task(run: uuid.UUID, key: str, status: str, output: dict[str, Any] | None) -> Task:
    capability = key.replace("tool.", "scan.") if key.startswith("tool.") else key
    return Task(run_id=run, key=key, name=key, phase="EXECUTION", capability=capability,
                status=status, output=output)  # fmt: skip


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    repo, other_repo = tmp_path / "repo", tmp_path / "other-repo"
    repo.mkdir()
    other_repo.mkdir()
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
            allowed_roots=(repo.resolve(), other_repo.resolve()),
            rate_limit_per_minute=100_000,
        )
    )
    srv.migrate()
    for name in USERS:
        srv.create_user(name, PASSWORD, is_admin=name == "admin")
    principals = {name: srv.login(name, PASSWORD) for name in USERS}
    owner = principals["owner"]
    assert owner is not None
    project = srv.create_project(owner, "project", path=str(repo))
    other = srv.create_project(owner, "other", path=str(other_repo))
    for name, role in ROLES.items():
        srv.set_member(owner, project, name, role)
    with srv.session() as s:
        run = Run(project_id=project, mode="AUDIT", state="COMPLETED", requested_by="owner",
                  options={"kind": "audit"})  # fmt: skip
        running = Run(project_id=project, mode="AUDIT", state="RUNNING", requested_by="owner")
        foreign = Run(project_id=other, mode="AUDIT", state="COMPLETED", requested_by="owner")
        s.add_all([run, running, foreign])
        s.flush()
        s.add_all([
            _task(run.id, "tool.semgrep", "SUCCEEDED", {"tool": "semgrep", "succeeded": True}),
            _task(run.id, "tool.osv-scanner", "SUCCEEDED",
                  {"tool": "osv-scanner", "succeeded": False, "skipped": True,
                   "error": SKIP_REASON}),
            _task(run.id, "tool.trivy", "FAILED", None),
            _task(run.id, "tool.gitleaks", "SUCCEEDED",
                  {"tool": "gitleaks", "succeeded": False, "error": TOOL_ERROR}),
            _task(run.id, "quality.gates", "SUCCEEDED",
                  {"gates": [{"gate": "security", "result": "FAIL", "blocking": True,
                              "reason": GATE_REASON}]}),
            _task(running.id, "tool.semgrep", "RUNNING", None),
        ])  # fmt: skip
        s.add_all([
            _finding(project, run.id, 1, source="gitleaks", severity="high", title=SECRET,
                     file_path=".env", evidence={"secret": SECRET, "match": SECRET}),
            _finding(project, run.id, 2, severity="high", title="Use of eval",
                     evidence={"code": CODE, "cwe": ["CWE-95: Eval Injection"]}),
            _finding(project, run.id, 3, source="trivy", severity="critical",
                     title="lodash 4.17.15: CVE-2020-8203", rule_id="CVE-2020-8203",
                     evidence={"package": "lodash", "installed": "4.17.15",
                               "fixed": "4.17.19", "class": "lang-pkgs",
                               "url": "https://avd.aquasec.com/nvd/cve-2020-8203"}),
            _finding(project, run.id, 4, source="trivy", severity="medium", title="DS026",
                     evidence={"class": "config", "resolution": "Add HEALTHCHECK",
                               "url": "javascript:alert(document.cookie)"}),
            _finding(project, run.id, 5, source="osv-scanner",
                     evidence={"package": "psr7", "aliases": ["CVE-2022-24775", "<script>"],
                               "url": "https://osv.dev/" + SECRET}),
            _finding(project, run.id, 6, status="suppressed"),
            _finding(project, None, 7, severity="critical", title="no run"),
            _finding(other, foreign.id, 8, severity="critical", title="other project"),
        ])  # fmt: skip
        ids = {"run": run.id, "running": running.id, "foreign": foreign.id}
    tokens = {name: srv.issue_token(p, "t")[0] for name, p in principals.items() if p}
    yield {"srv": srv, "project": project, "other": other, "tokens": tokens,
           "principals": principals, **ids}  # fmt: skip
    srv.close()


@pytest.fixture
def client(world: dict[str, Any]) -> Iterator[TestClient]:
    with TestClient(create_app(world["srv"], broker=None, security_center_enabled=True)) as c:
        c.headers["Authorization"] = f"Bearer {world['tokens']['owner']}"
        yield c


def _url(world: dict[str, Any], project: str = "project", run: str = "run") -> str:
    return f"/api/v1/projects/{world[project]}/security-center/runs/{world[run]}"


def _all_rows(client: TestClient, url: str) -> list[dict[str, Any]]:
    return list(client.get(url, params={"limit": 500}).json()["findings"])


# ------------------------------------------------------------------ switch --
def test_disabled_by_default(world: dict[str, Any]) -> None:
    assert world["srv"].settings.security_center is False
    with TestClient(create_app(world["srv"], broker=None)) as c:
        c.headers["Authorization"] = f"Bearer {world['tokens']['owner']}"
        assert "security-center" not in c.get("/api/v1/openapi.json").text
        assert c.get(_url(world)).status_code == 404
        assert c.get(f"{_url(world)}/findings/{uuid.uuid4()}").status_code == 404
        assert c.get("/api/v1/me").json()["features"] == {"security_center": False}


def test_enabled_by_the_server_setting(world: dict[str, Any]) -> None:
    srv = world["srv"]
    srv.settings = replace(srv.settings, security_center=True)
    with TestClient(create_app(srv, broker=None)) as c:
        c.headers["Authorization"] = f"Bearer {world['tokens']['auditor']}"
        assert "security-center" in c.get("/api/v1/openapi.json").text
        assert c.get(_url(world)).status_code == 200
        assert c.get("/api/v1/me").json()["features"] == {"security_center": True}
    with TestClient(create_app(srv, broker=None, security_center_enabled=False)) as c:
        assert c.get(_url(world)).status_code == 404  # an explicit off wins


# ----------------------------------------------------------- access rights --
def test_requires_authentication(world: dict[str, Any], client: TestClient) -> None:
    del client.headers["Authorization"]
    assert client.get(_url(world)).status_code == 401
    client.headers["Authorization"] = "Bearer not-a-token"
    assert client.get(_url(world)).status_code == 401


@pytest.mark.parametrize("username", ["owner", "maintainer", "developer", "auditor"])
def test_members_read(world: dict[str, Any], client: TestClient, username: str) -> None:
    client.headers["Authorization"] = f"Bearer {world['tokens'][username]}"
    assert client.get(_url(world)).json()["summary"]["total"] == 6


@pytest.mark.parametrize("username", ["outsider", "admin"])
def test_non_members_get_nothing(world: dict[str, Any], client: TestClient, username: str) -> None:
    """Instance administrators have no implicit access to project data (least privilege)."""
    url = _url(world)
    finding = _all_rows(client, url)[0]["id"]
    client.headers["Authorization"] = f"Bearer {world['tokens'][username]}"
    assert client.get(url).status_code == 404
    assert client.get(f"{url}/findings/{finding}").status_code == 404
    with pytest.raises(NotFoundError):
        world["srv"].security_center(world["principals"][username], world["project"], world["run"])


def test_auditor_is_read_only(world: dict[str, Any], client: TestClient) -> None:
    client.headers["Authorization"] = f"Bearer {world['tokens']['auditor']}"
    url = _url(world)
    finding = _all_rows(client, url)[0]["id"]
    for target in (url, f"{url}/findings/{finding}"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, target, json={}).status_code == 405


def test_token_limits(world: dict[str, Any], client: TestClient) -> None:
    srv, owner = world["srv"], world["principals"]["owner"]
    scoped, _ = srv.issue_token(owner, "other only", project_id=world["other"])
    client.headers["Authorization"] = f"Bearer {scoped}"
    assert client.get(_url(world)).status_code == 404  # scoped to another project
    assert client.get(_url(world, "other", "foreign")).status_code == 200
    agent, _ = srv.issue_token(owner, "agent", project_id=world["project"], max_role=Role.AGENT)
    capped, _ = srv.issue_token(owner, "read", max_role=Role.AUDITOR)
    for token in (agent, capped):
        client.headers["Authorization"] = f"Bearer {token}"
        assert client.get(_url(world)).status_code == 200
        assert client.post(_url(world), json={}).status_code == 405
    with pytest.raises(NotFoundError):
        srv.security_center(replace(owner, token_project_id=world["other"]),
                            world["project"], world["run"])  # fmt: skip


def test_foreign_runs_and_findings(world: dict[str, Any], client: TestClient) -> None:
    url = _url(world)
    assert client.get(_url(world, "project", "foreign")).status_code == 404
    assert client.get(_url(world, "other", "run")).status_code == 404
    assert client.get(f"{url.rsplit('/', 1)[0]}/{uuid.uuid4()}").status_code == 404
    foreign_rows = _all_rows(client, _url(world, "other", "foreign"))
    assert [r["title"] for r in foreign_rows] == ["other project"]
    assert client.get(f"{url}/findings/{foreign_rows[0]['id']}").status_code == 404
    assert client.get(f"{url}/findings/{uuid.uuid4()}").status_code == 404
    own = _all_rows(client, url)[0]["id"]
    running = _url(world, "project", "running")
    assert client.get(f"{running}/findings/{own}").status_code == 404  # another run
    assert client.get(f"{url}/findings/not-a-uuid").status_code == 422


# ------------------------------------------------------- run, coverage, gate --
def test_overview_reports_real_states(world: dict[str, Any], client: TestClient) -> None:
    data = client.get(_url(world)).json()
    assert data["schema_version"] == "2"
    assert data["run"] | {"created_at": None} == {
        "state": "COMPLETED", "finished": True, "mode": "AUDIT", "kind": "audit",
        "created_at": None, "finished_at": None,
    }  # fmt: skip
    cov = data["coverage"]
    assert cov["sast"]["state"] == "completed"
    assert cov["secrets"] == {
        "available": True,
        "state": "error",
        "tools": [{"tool": "gitleaks", "state": "error", "reason": "tool_failed"}],
    }
    assert cov["sca"]["state"] == "error"
    assert cov["sca"]["tools"] == [
        {"tool": "osv-scanner", "state": "skipped", "reason": "offline_db_missing"},
        {"tool": "trivy", "state": "error", "reason": "task_failed"},
    ]
    assert cov["iac"]["state"] == "error"
    assert cov["dast"] == {"available": False, "state": "unsupported", "tools": []}
    assert cov["container"]["state"] == "unsupported"
    assert data["capabilities"] == {"dast": "not_implemented", "container": "not_implemented"}
    assert data["security_gate"] == {"result": "FAIL", "blocking": True}
    assert data["compliance"] == {"state": "not_assessed", "attestation": False}
    assert data["ai_fix"] == {"state": "disabled", "requires_human_review": True}
    assert data["summary"] == {
        "total": 6,
        "by_severity": {"critical": 1, "high": 2, "medium": 1, "low": 2, "info": 0},
        "by_source": {"gitleaks": 1, "osv-scanner": 1, "semgrep": 2, "trivy": 2},
        "by_status": {"open": 5, "suppressed": 1},
    }


def test_unfinished_run_without_results(world: dict[str, Any], client: TestClient) -> None:
    """No findings and no completed scans: an empty page, never a clean bill of health."""
    data = client.get(_url(world, "project", "running")).json()
    assert data["run"]["finished"] is False
    assert data["findings"] == []
    assert data["summary"]["total"] == 0
    assert data["page"]["total"] == 0
    assert data["security_gate"] is None
    assert data["coverage"]["sast"]["tools"] == [
        {"tool": "semgrep", "state": "running", "reason": None}
    ]
    assert data["coverage"]["secrets"]["tools"] == [
        {"tool": "gitleaks", "state": "not_run", "reason": "not_planned"}
    ]
    assert {c["state"] for c in data["coverage"].values()} == {"incomplete", "unsupported"}


# ------------------------------------------------- filters and pagination --
def test_pagination(world: dict[str, Any], client: TestClient) -> None:
    url = _url(world)
    first = client.get(url, params={"limit": 4}).json()
    second = client.get(url, params={"limit": 4, "offset": 4}).json()
    assert first["page"] == {
        "limit": 4, "offset": 0, "total": 6,
        "filters": {"severity": None, "source": None, "status": None},
    }  # fmt: skip
    assert len(first["findings"]) == 4 and len(second["findings"]) == 2
    ids = [r["id"] for r in first["findings"] + second["findings"]]
    assert len(set(ids)) == 6
    assert client.get(url, params={"offset": 6}).json()["findings"] == []


@pytest.mark.parametrize(
    ("params", "titles"),
    [
        ({"severity": "high"}, {"Secret detected", "Use of eval"}),
        ({"source": "trivy"}, {"lodash 4.17.15: CVE-2020-8203", "DS026"}),
        ({"status": "suppressed"}, {"finding 6"}),
        ({"severity": "low", "source": "semgrep"}, {"finding 6"}),
        ({"severity": "info"}, set()),
    ],
)
def test_filters(
    world: dict[str, Any], client: TestClient, params: dict[str, str], titles: set[str]
) -> None:
    data = client.get(_url(world), params=params).json()
    assert {r["title"] for r in data["findings"]} == titles
    assert data["page"]["total"] == len(titles)
    assert data["summary"]["total"] == 6  # counts describe the whole run, not the filter


@pytest.mark.parametrize(
    "params",
    [
        {"severity": "urgent"}, {"source": "../etc"}, {"source": "A" * 101},
        {"status": "open; drop table"}, {"limit": 0}, {"limit": 501}, {"offset": -1},
        {"offset": 100_001},
    ],
)  # fmt: skip
def test_invalid_parameters(
    world: dict[str, Any], client: TestClient, params: dict[str, Any]
) -> None:
    assert client.get(_url(world), params=params).status_code == 422


# ---------------------------------------------------------- safe content --
def test_no_secret_material_anywhere(world: dict[str, Any], client: TestClient) -> None:
    url = _url(world)
    rows = _all_rows(client, url)
    bodies = [client.get(url, params={"limit": 500}).text]
    bodies += [client.get(f"{url}/findings/{r['id']}").text for r in rows]
    for body in bodies:
        for leak in (SECRET, CODE, "hunter2", TOOL_ERROR, SKIP_REASON, "/home/alice",
                     GATE_REASON, '"message"', '"evidence"', "javascript:"):  # fmt: skip
            assert leak not in body
    secret = next(r for r in rows if r["source"] == "gitleaks")
    assert secret["title"] == "Secret detected" and secret["category"] == "secrets"
    assert secret["file_path"] == ".env" and secret["line"] == 1


def test_details_are_allowlisted(world: dict[str, Any], client: TestClient) -> None:
    url = _url(world)
    rows = {r["title"]: r for r in _all_rows(client, url)}

    def details(title: str) -> dict[str, Any]:
        response = client.get(f"{url}/findings/{rows[title]['id']}")
        assert response.status_code == 200
        return dict(response.json())

    sca = details("lodash 4.17.15: CVE-2020-8203")
    assert sca["category"] == "sca"
    assert sca["details"] == {
        "package": "lodash", "installed_version": "4.17.15", "fixed_version": "4.17.19",
        "reference": "https://avd.aquasec.com/nvd/cve-2020-8203",
    }  # fmt: skip
    assert "scanner_message" in sca["withheld"] and "raw_evidence" in sca["withheld"]
    iac = details("DS026")
    assert iac["category"] == "iac"
    assert iac["details"] == {"resolution": "Add HEALTHCHECK"}  # javascript: URL dropped
    osv = details("finding 5")
    assert osv["details"] == {"package": "psr7", "aliases": ["CVE-2022-24775"]}
    sast = details("Use of eval")
    assert sast["details"] == {"cwe": ["CWE-95: Eval Injection"]}  # never the code
    assert details("Secret detected")["details"] == {}
