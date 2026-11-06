"""Per-test results of runs from their JUnit reports (ADR-0044): the API end to end."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.core import detached_project
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import Role
from ai_coding.orchestrator.engine import make_artifact_store
from ai_coding.persistence.models import Artifact, Run, Task
from ai_coding.persistence.types import utcnow

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


def report(*cases: tuple[str, str, str]) -> bytes:
    """A pytest-style report: (name, status, failing line) per case."""
    body = []
    for name, status, line in cases:
        inner = ""
        if status == "failed":
            inner = (
                f'<failure message="{name} broke">tests/test_m.py:{line}: AssertionError</failure>'
            )
        elif status == "skipped":
            inner = '<skipped message="later"/>'
        body.append(f'<testcase classname="tests.test_m" name="{name}" file="tests/test_m.py" '
                    f'line="1" time="0.{len(body) + 1}">{inner}</testcase>')  # fmt: skip
    xml = f'<testsuites><testsuite name="pytest">{"".join(body)}</testsuite></testsuites>'
    return xml.encode()


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Project alpha of ``dev`` (``aud`` audits it) with runs, oldest first:

    r1 develop: a, b pass, c fails · r2 audit: its test tool did not run · r3 develop: a
    baseline and two implement reports (the latest: a fails, b and c pass) · r4 develop:
    tests skipped by the gate · r5 develop: a hostile report · r6 discover. gamma of
    ``outsider``.
    """
    tmp = tmp_path_factory.mktemp("tests")
    roots = tmp / "repos"
    for name in ("alpha", "gamma"):
        (roots / name).mkdir(parents=True)
        (roots / name / "main.py").write_text("print(1)\n", encoding="utf-8")
    srv = Server(ServerSettings(data_dir=tmp / "data", database_url=f"sqlite:///{tmp / 's.db'}",
                                allowed_roots=(roots.resolve(),),
                                rate_limit_per_minute=100_000))  # fmt: skip
    srv.migrate()
    for name in ("dev", "aud", "outsider"):
        srv.create_user(name, PASSWORD)
    dev, outsider = srv.login("dev", PASSWORD), srv.login("outsider", PASSWORD)
    assert dev is not None and outsider is not None
    ids: dict[str, Any] = {"server": srv}
    ids["alpha"] = srv.create_project(dev, "alpha", path=str(roots.resolve() / "alpha"))
    ids["gamma"] = srv.create_project(outsider, "gamma", path=str(roots.resolve() / "gamma"))
    srv.set_member(dev, ids["alpha"], "aud", Role.AUDITOR)
    with srv.session() as s:
        project = detached_project(s, ids["alpha"])
    root, config = srv.project_config(project)
    store = make_artifact_store(config, root)
    start = utcnow() - timedelta(hours=1)
    plan: list[tuple[str, str, list[tuple[str, bytes]], dict[str, Any] | None]] = [
        ("r1", "develop", [("junit-implement.xml", report(("a", "passed", ""), ("b", "passed", ""),
                                                          ("c", "failed", "30")))], None),
        ("r2", "audit", [], {"tool": "go-test", "category": "tests", "succeeded": False,
                             "skipped": True, "error": "unavailable: go not found"}),
        ("r3", "develop", [
            ("junit-baseline.xml", report(("a", "passed", ""), ("b", "failed", "12"))),
            ("junit-implement.xml", report(("a", "passed", ""))),
            ("junit-implement.xml", report(("a", "failed", "7"), ("b", "passed", ""),
                                           ("c", "passed", ""), ("d", "skipped", ""))),
        ], None),
        ("r4", "develop", [], {"tests": {"status": "skipped", "reason": "tests gate disabled"}}),
        ("r5", "develop", [("junit-implement.xml",
                            b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')],
         None),
        ("r6", "discover", [], None),
    ]  # fmt: skip
    with srv.session() as s:
        for n, (key, kind, reports, output) in enumerate(plan):
            at = start + timedelta(minutes=n)
            run = Run(project_id=ids["alpha"], mode="audit" if kind == "discover" else kind,
                      goal=f"{key} goal", state="SUCCEEDED", requested_by="dev",
                      options={"kind": kind}, created_at=at)  # fmt: skip
            s.add(run)
            s.flush()
            ids[key] = run.id
            s.add(Task(run_id=run.id, key="scan.go-test" if kind == "audit" else "implement",
                       name="step", phase="execution", capability="x", status="SUCCEEDED",
                       output=output or {}))  # fmt: skip
            for i, (name, data) in enumerate(reports):
                ref = store.put(data)
                s.add(Artifact(run_id=run.id, name=name, media_type="text/xml",
                               sha256=ref.sha256, size_bytes=ref.size_bytes, uri=ref.uri,
                               created_at=at + timedelta(seconds=i)))  # fmt: skip
        foreign = Run(project_id=ids["gamma"], mode="develop", goal="g", state="SUCCEEDED",
                      requested_by="outsider", options={"kind": "develop"})  # fmt: skip
        s.add(foreign)
        s.flush()
        ids["foreign"] = foreign.id
    with TestClient(create_app(srv)) as client:
        ids["client"] = client
        for name in ("dev", "aud", "outsider"):
            r = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
            ids[name] = {"Authorization": f"Bearer {r.json()['token']}"}
        yield ids
    srv.close()


def _get(world: dict[str, Any], who: str, path: str, status: int = 200, **params: Any) -> Any:
    r = world["client"].get(f"/api/v1{path}", headers=world[who], params=params)
    assert r.status_code == status, (path, r.status_code, r.text)
    return r.json()


def test_run_results_latest_report_failed_first_and_regressions(world: dict[str, Any]) -> None:
    data = _get(world, "dev", f"/runs/{world['r3']}/tests")
    assert data["totals"] == {"tests": 4, "passed": 2, "failed": 1, "errors": 0, "skipped": 1,
                              "time": 1.0}  # fmt: skip
    assert data["reason"] is None and data["problems"] == []
    assert [(r["name"], r["primary"]) for r in data["reports"]] == [
        ("junit-baseline.xml", False), ("junit-implement.xml", True)]  # fmt: skip
    assert data["reports"][0]["totals"]["failed"] == 1  # the baseline is listed, not counted
    (suite,) = data["suites"]
    first = suite["cases"][0]
    assert (first["name"], first["status"], first["file"], first["line"]) == (
        "a", "failed", "tests/test_m.py", 7)  # fmt: skip
    assert first["message"] == "a broke" and "AssertionError" in first["details"]
    assert [c["status"] for c in suite["cases"]] == ["failed", "passed", "passed", "skipped"]
    assert data["compared_with"] == str(world["r1"])
    assert [r["key"] for r in data["regressions"]] == ["tests.test_m::a"]
    assert data["fixed"] == ["tests.test_m::c"]


def test_runs_without_results_say_why(world: dict[str, Any]) -> None:
    audit = _get(world, "dev", f"/runs/{world['r2']}/tests")
    assert audit["suites"] == [] and audit["totals"]["tests"] == 0
    assert "go-test: unavailable: go not found" in audit["reason"]
    skipped = _get(world, "aud", f"/runs/{world['r4']}/tests")  # an auditor may read them
    assert skipped["reason"] == "tests skipped: tests gate disabled"
    assert skipped["setting"] == "quality_gates.tests.enabled"
    hostile = _get(world, "dev", f"/runs/{world['r5']}/tests")
    assert hostile["suites"] == [] and "DOCTYPE" in hostile["problems"][0]
    assert hostile["reports"] == [{"name": "junit-implement.xml", "primary": True,
                                   "created_at": hostile["reports"][0]["created_at"]}]  # fmt: skip
    discover = _get(world, "dev", f"/runs/{world['r6']}/tests")
    assert discover["reason"] == "a discover run does not run tests"


def test_project_test_runs(world: dict[str, Any]) -> None:
    runs = _get(world, "dev", f"/projects/{world['alpha']}/test-runs")
    assert [r["run_id"] for r in runs] == [str(world[k]) for k in ("r5", "r4", "r3", "r2", "r1")]
    r3 = runs[2]
    assert "suites" not in r3 and r3["totals"]["failed"] == 1
    assert [(f["name"], f["line"]) for f in r3["failing"]] == [("a", 7)]
    assert [r["name"] for r in r3["regressions"]] == ["a"]
    assert runs[4]["compared_with"] is None and runs[4]["regressions"] == []
    assert runs[1]["setting"] == "quality_gates.tests.enabled"
    assert len(_get(world, "dev", f"/projects/{world['alpha']}/test-runs", limit=2)) == 2
    assert len(_get(world, "dev", f"/projects/{world['alpha']}/test-runs", limit=999)) == 5


def test_history_of_one_test(world: dict[str, Any]) -> None:
    path = f"/projects/{world['alpha']}/tests/history"
    rows = _get(world, "dev", path, test="tests.test_m::a")
    assert [(r["run_id"], r["status"]) for r in rows] == [
        (str(world["r3"]), "failed"), (str(world["r1"]), "passed")]  # fmt: skip
    assert rows[0]["message"] == "a broke"
    gone = _get(world, "dev", path, test="tests.test_m::zzz")
    assert {r["status"] for r in gone} == {"absent"}
    _get(world, "dev", path, status=422)  # the test is required — checked after access
    _get(world, "dev", path, status=422, test="   ")


def test_outsiders_see_nothing(world: dict[str, Any]) -> None:
    for path in (f"/runs/{world['r3']}/tests", f"/projects/{world['alpha']}/test-runs",
                 f"/projects/{world['alpha']}/tests/history"):  # fmt: skip
        _get(world, "outsider", path, status=404)  # even without ?test=
        _get(world, "outsider", path, status=404, test="tests.test_m::a")
    _get(world, "dev", f"/runs/{world['foreign']}/tests", status=404)
    r = world["client"].get(f"/api/v1/runs/{world['r3']}/tests")
    assert r.status_code == 401
