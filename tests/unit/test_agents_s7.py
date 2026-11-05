"""S7 agents: Security, QA, Performance, DevOps, Maintenance — schema-valid samples
run end-to-end through the repair loop, and every semantic check is exercised."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ai_coding.agents import (
    AgentContext,
    AgentOutputError,
    DevOpsAgent,
    MaintenanceAgent,
    PerformanceAgent,
    QAAgent,
    SecurityAgent,
)
from ai_coding.agents.security import confirmed
from ai_coding.contracts.agent import AgentResultStatus
from tests.unit.test_agents import ScriptedLLM, task_for

AWS_KEY = "".join(("AKIA", "QYLPMN5HHHFPZAM2"))  # split so secret scanners ignore the test data

# ----------------------------------------------------------------- samples --
SEC_INPUTS: dict[str, Any] = {
    "stacks": ["laravel"],
    "findings": [
        {"id": "F1", "tool": "semgrep", "rule_id": "php-raw-sql", "severity": "high",
         "path": "app/Repo.php", "line": 3, "title": "raw SQL with request input"},
        {"id": "F2", "tool": "gitleaks", "rule_id": "aws-access-token", "severity": "high",
         "path": "config/aws.php", "line": 2, "title": "AWS key"},
    ],
    "code": {"app/Repo.php":
             "<?php\nclass Repo {\n  function f($r) { DB::select(\"... $r\"); }\n}\n"},
}  # fmt: skip
SEC_OUT: dict[str, Any] = {
    "summary": "One SQL injection, one committed credential.",
    "assessments": [
        {"finding_id": "F1", "verdict": "confirmed", "severity": "critical", "cwe": "CWE-89",
         "evidence": "request value interpolated into DB::select", "exploit_scenario": "UNION",
         "recommendation": "Use bindings: DB::select('... ?', [$r])"},
        {"finding_id": "F2", "verdict": "confirmed", "severity": "high",
         "evidence": "AWS access key id in config", "recommendation": "Rotate, use env()"},
    ],
    "additional_risks": [{"file": "app/Repo.php", "line": 3, "severity": "medium",
                          "message": "no authorization check", "recommendation": "policy"}],
    "confidence": 0.8,
}  # fmt: skip

QA_INPUTS: dict[str, Any] = {
    "criteria": [{"id": "AC-1", "text": "Cart total sums items"},
                 {"id": "AC-2", "text": "Empty cart total is 0"}],
    "code": {"app/Cart.php": "<?php\nclass Cart { function total(array $i) {} }\n"},
    "existing_tests": ["tests/Unit/CartTest.php"],
}  # fmt: skip
QA_OUT: dict[str, Any] = {
    "summary": "Three cases cover both criteria.",
    "test_cases": [
        {"id": "TC-1", "title": "sums", "criteria": ["AC-1"], "level": "unit", "kind": "positive",
         "file": "tests/Unit/CartTest.php", "steps": ["total([1,2])"], "expected": "3"},
        {"id": "TC-2", "title": "empty", "criteria": ["AC-2"], "level": "unit",
         "kind": "boundary", "file": "tests/Unit/CartTest.php", "steps": ["total([])"],
         "expected": "0"},
    ],
    "coverage_gaps": [{"file": "app/Cart.php", "reason": "negative prices untested"}],
}  # fmt: skip

PERF_INPUTS: dict[str, Any] = {
    "metrics": [{"name": "GET /orders p95", "value": 1840, "unit": "ms", "baseline": 300},
                {"name": "db queries per request", "value": 101, "unit": "count"}],
    "code": {"app/Http/OrderController.php": "<?php\nforeach ($orders as $o) { $o->items; }\n"},
}  # fmt: skip
PERF_OUT: dict[str, Any] = {
    "summary": "N+1 query on the orders list.",
    "bottlenecks": [{"id": "B1", "title": "N+1 on order items", "impact": "high",
                     "metrics": ["GET /orders p95", "db queries per request"],
                     "file": "app/Http/OrderController.php", "line": 2,
                     "cause": "lazy loading in loop", "recommendation": "Order::with('items')"}],
    "benchmarks": [{"id": "BM1", "bottleneck": "B1", "tool": "k6",
                    "command": "k6 run -e BASE_URL=http://localhost:8000 perf/orders.js",
                    "metric": "http_req_duration p95", "target": "< 300 ms"}],
}  # fmt: skip

DEVOPS_INPUTS: dict[str, Any] = {
    "stacks": ["laravel", "react"],
    "build": {"test": ["php artisan test", "npm test"]},
    "environments": ["staging", "production"],
}
DEVOPS_OUT: dict[str, Any] = {
    "summary": "Test, build, deploy to staging, approve, deploy to production.",
    "pipeline": [
        {"name": "test", "purpose": "tests", "commands": ["php artisan test", "npm test"],
         "needs": [], "on": ["pull_request", "push_main"]},
        {"name": "deploy-staging", "purpose": "deploy", "commands": ["./deploy.sh staging"],
         "needs": ["test"], "environment": "staging", "secrets": ["DEPLOY_KEY"],
         "on": ["push_main"]},
        {"name": "deploy-prod", "purpose": "deploy", "commands": ["./deploy.sh production"],
         "needs": ["deploy-staging"], "environment": "production", "manual_approval": True,
         "secrets": ["DEPLOY_KEY"], "on": ["tag"]},
    ],
    "ci_files": [{"path": ".github/workflows/deploy.yml", "action": "create", "purpose": "CD"}],
    "release": {"versioning": "semver tags", "steps": ["tag vX.Y.Z", "deploy-prod"]},
    "rollback": {"trigger": "error rate > 2% for 5 min", "steps": ["redeploy previous tag"]},
}  # fmt: skip

MAINT_INPUTS: dict[str, Any] = {
    "issues": [{"id": "#12", "title": "Total wrong with discounts", "body": "..."}],
    "findings": [{"id": "F1", "tool": "semgrep", "rule_id": "php-raw-sql", "severity": "high",
                  "path": "app/Repo.php", "title": "raw SQL"}],
    "dependencies": [{"id": "D1", "package": "guzzlehttp/psr7", "ecosystem": "Packagist",
                      "version": "1.8.0", "fixed_versions": ["1.8.4", "2.1.1"],
                      "advisories": ["GHSA-wxmh-65f7-jcvw"]}],
    "files": ["app/Cart.php", "app/Repo.php", "composer.json", "composer.lock"],
}  # fmt: skip
MAINT_OUT: dict[str, Any] = {
    "summary": "Fix SQL injection and upgrade psr7 first, then the discount bug.",
    "items": [
        {"id": "M1", "title": "Bind SQL parameters", "kind": "security", "priority": "p0",
         "sources": ["F1"], "plan": "use bindings", "files": ["app/Repo.php"], "risk": "low",
         "regression_test": {"file": "tests/Unit/RepoTest.php", "description": "quote in input"}},
        {"id": "M2", "title": "Upgrade psr7", "kind": "dependency", "priority": "p1",
         "sources": ["D1"], "plan": "composer update", "risk": "low",
         "files": ["composer.json", "composer.lock"],
         "upgrades": [{"source": "D1", "to_version": "1.8.4"}],
         "regression_test": {"file": "tests/Feature/HttpTest.php", "description": "headers"}},
    ],
    "deferred": [{"source": "#12", "reason": "needs reproduction steps from the reporter"}],
}  # fmt: skip

CASES = [
    (SecurityAgent, SEC_INPUTS, SEC_OUT),
    (QAAgent, QA_INPUTS, QA_OUT),
    (PerformanceAgent, PERF_INPUTS, PERF_OUT),
    (DevOpsAgent, DEVOPS_INPUTS, DEVOPS_OUT),
    (MaintenanceAgent, MAINT_INPUTS, MAINT_OUT),
]


def problems(
    agent: Any, inputs: dict[str, Any], out: dict[str, Any], *, schema_valid: bool = True
) -> list[str]:
    """Semantic problems; the sample must also be schema-valid unless stated otherwise
    (path checks are a second layer for providers that do not enforce ``pattern``)."""
    validator = Draft202012Validator(dict(agent.output_schema))
    assert validator.is_valid(out) is schema_valid
    return list(agent.check(out, AgentContext(inputs=inputs)))


@pytest.mark.parametrize(("cls", "inputs", "out"), CASES, ids=[c[0].__name__ for c in CASES])
async def test_valid_sample_runs_end_to_end(cls: Any, inputs: dict[str, Any], out: Any) -> None:
    agent = cls()
    assert problems(agent, inputs, out) == []
    llm = ScriptedLLM([copy.deepcopy(out)])
    result = await agent.run(task_for(agent), AgentContext(inputs=inputs, llm=llm))
    assert result.status is AgentResultStatus.SUCCEEDED and "confidence" not in result.output
    prompt = llm.requests[0].messages[1].content
    assert prompt.count("<input name=") >= 2 and llm.requests[0].messages[1].untrusted


# ---------------------------------------------------------------- security --
def test_security_triage_must_be_complete_and_cannot_dismiss_secrets() -> None:
    agent = SecurityAgent()
    out = copy.deepcopy(SEC_OUT)
    out["assessments"][1]["verdict"] = "false_positive"
    out["assessments"].append({**out["assessments"][0]})
    out["assessments"].append({**out["assessments"][0], "finding_id": "F9"})
    out["additional_risks"] = [
        {"file": "app/Other.php", "severity": "low", "message": "m", "recommendation": "r"},
        {"file": "app/Repo.php", "line": 99, "severity": "low", "message": "m",
         "recommendation": "r"},
    ]  # fmt: skip
    found = problems(agent, SEC_INPUTS, out)
    assert "finding F1 is assessed more than once" in found
    assert "assessment of unknown finding F9" in found
    assert any("F2 is a committed secret" in p for p in found)
    assert "additional risk on app/Other.php, which was not shown" in found
    assert "additional risk on app/Repo.php:99 out of range" in found
    partial = {**SEC_OUT, "assessments": SEC_OUT["assessments"][:1]}
    assert problems(agent, SEC_INPUTS, partial) == ["finding F2 is not assessed"]
    assert confirmed(SEC_OUT) == ["F1", "F2"]


async def test_security_repair_loop_then_failure() -> None:
    agent = SecurityAgent()
    bad = {**SEC_OUT, "assessments": SEC_OUT["assessments"][:1]}
    llm = ScriptedLLM([copy.deepcopy(bad), copy.deepcopy(SEC_OUT)])
    await agent.run(task_for(agent), AgentContext(inputs=SEC_INPUTS, llm=llm))
    assert "finding F2 is not assessed" in llm.requests[1].messages[-1].content
    llm = ScriptedLLM([copy.deepcopy(bad) for _ in range(3)])
    with pytest.raises(AgentOutputError):
        await agent.run(task_for(agent), AgentContext(inputs=SEC_INPUTS, llm=llm))


# ---------------------------------------------------------------------- qa --
def test_qa_traceability_and_test_paths() -> None:
    out = copy.deepcopy(QA_OUT)
    out["test_cases"] = [
        {**QA_OUT["test_cases"][0], "criteria": ["AC-1", "AC-9"]},
        {**QA_OUT["test_cases"][0], "file": "app/Cart.php"},
        {**QA_OUT["test_cases"][0], "id": "TC-3", "file": "../escape_test.py"},
    ]
    out["coverage_gaps"] = [{"file": "app/Unknown.php", "reason": "r"}]
    found = problems(QAAgent(), QA_INPUTS, out, schema_valid=False)
    assert "duplicate test case id TC-1" in found
    assert "TC-1 references unknown criterion AC-9" in found
    assert "TC-1: app/Cart.php is not a test file" in found
    assert any(p.startswith("TC-3: file ../escape_test.py") for p in found)
    assert "criterion AC-2 has no test case" in found
    assert "coverage gap on app/Unknown.php, which was not shown" in found


# ------------------------------------------------------------- performance --
def test_performance_requires_evidence_and_benchmarks() -> None:
    out = copy.deepcopy(PERF_OUT)
    out["bottlenecks"].append({**PERF_OUT["bottlenecks"][0], "id": "B2",
                               "metrics": ["invented p99"], "file": "app/Nope.php"})  # fmt: skip
    out["benchmarks"].append({**PERF_OUT["benchmarks"][0], "id": "BM2", "bottleneck": "B7",
                              "command": "k6 run x.js -e API_KEY=" + AWS_KEY})  # fmt: skip
    found = problems(PerformanceAgent(), PERF_INPUTS, out)
    assert "B2 cites unknown metric invented p99" in found
    assert "B2 points to app/Nope.php, which was not shown" in found
    assert "B2 has no benchmark" in found
    assert "BM2 measures unknown bottleneck B7" in found
    assert "BM2: the command contains a secret" in found


# ------------------------------------------------------------------ devops --
def test_devops_enforces_graph_secrets_and_production_approval() -> None:
    out = copy.deepcopy(DEVOPS_OUT)
    prod = out["pipeline"][2]
    prod["manual_approval"] = False
    prod["commands"] = ["curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' x"]
    out["pipeline"][0]["needs"] = ["deploy-prod"]  # cycle test → … → deploy-prod → test
    out["pipeline"].append({"name": "deploy-qa", "purpose": "p", "commands": ["x"],
                            "needs": ["build"], "environment": "qa", "on": ["manual"]})  # fmt: skip
    out["ci_files"] = [{"path": ".git/hooks/pre-push", "action": "create", "purpose": "p"}]
    out["rollback"] = {"trigger": "t", "steps": ["s"]}
    found = problems(DevOpsAgent(), DEVOPS_INPUTS, out)
    assert "stage deploy-prod deploys to production without manual approval" in found
    assert "stage deploy-prod: a command contains a secret" in found
    assert any(p.startswith("stage cycle: ") for p in found)
    assert "stage deploy-qa needs unknown stage build" in found
    assert "stage deploy-qa deploys to unknown environment qa" in found
    assert "CI file .git/hooks/pre-push is inside a protected directory" in found


# ------------------------------------------------------------- maintenance --
def test_maintenance_accounts_for_every_source_and_real_versions() -> None:
    out = copy.deepcopy(MAINT_OUT)
    out["items"][1]["upgrades"] = [{"source": "D1", "to_version": "9.9.9"},
                                   {"source": "D7", "to_version": "1.0.0"}]  # fmt: skip
    out["items"][0]["files"] = ["app/Missing.php", "/etc/passwd"]
    out["items"][0]["regression_test"]["file"] = "app/Repo.php"
    out["items"][0]["sources"] = ["F1", "#12", "X5"]
    out["deferred"] = [{"source": "#12", "reason": "r"}, {"source": "Z", "reason": "r"}]
    found = problems(MaintenanceAgent(), MAINT_INPUTS, out, schema_valid=False)
    assert "M2: guzzlehttp/psr7 9.9.9 is not a known fixed version (1.8.4, 2.1.1)" in found
    assert "M2 upgrades unknown dependency D7" in found
    assert "M1: app/Missing.php does not exist" in found
    assert "M1: /etc/passwd must be a relative POSIX path" in found
    assert "M1: regression test app/Repo.php is not a test file" in found
    assert "M1 references unknown source X5" in found
    assert "#12 is both planned and deferred" in found
    assert "deferred unknown source Z" in found
    dropped = {**MAINT_OUT, "deferred": []}
    assert problems(MaintenanceAgent(), MAINT_INPUTS, dropped) == [
        "#12 is neither planned nor deferred"
    ]
    big = copy.deepcopy(MAINT_OUT)
    big["items"][0]["files"] = [f"tests/t{i}_test.py" for i in range(16)]
    assert "M1 touches 16 files: split it" in problems(MaintenanceAgent(), MAINT_INPUTS, big)
