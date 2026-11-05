"""SARIF 2.1.0 and JUnit exports of an audit report."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from ai_coding.reports import render_junit, render_sarif, to_sarif

REPORT = {
    "project": "demo",
    "verdict": "FAIL",
    "gates": [
        {
            "gate": "security",
            "result": "FAIL",
            "blocking": True,
            "reason": "1 finding",
            "evidence": ["semgrep:eval a.py:3 (high)"],
        },
        {"gate": "static", "result": "PASS", "blocking": True, "reason": "ok", "evidence": []},
        {
            "gate": "tests",
            "result": "SKIPPED",
            "blocking": True,
            "reason": "no tests tool",
            "evidence": [],
        },
    ],
    "tools": [
        {
            "tool": "semgrep",
            "category": "sast",
            "version": "1.183.0",
            "succeeded": True,
            "error": None,
            "duration_seconds": 2.5,
            "findings": [],
        },
        {
            "tool": "ruff",
            "category": "lint",
            "version": None,
            "succeeded": False,
            "error": "image missing",
            "duration_seconds": 0.0,
            "findings": [],
        },
    ],
    "findings": [
        {
            "tool": "semgrep",
            "rule_id": "python-eval-exec",
            "severity": "high",
            "title": "eval",
            "message": "eval is dangerous",
            "path": "a.py",
            "line": 3,
            "fingerprint": "f" * 64,
            "status": "new",
        },
        {
            "tool": "semgrep",
            "rule_id": "python-weak-hash",
            "severity": "medium",
            "title": "md5",
            "message": "",
            "path": "b.py",
            "line": None,
            "fingerprint": "e" * 64,
            "status": "baselined",
        },
        {
            "tool": "semgrep",
            "rule_id": "python-eval-exec",
            "severity": "high",
            "title": "eval",
            "message": "x",
            "path": None,
            "line": None,
            "fingerprint": "d" * 64,
            "status": "new",
        },
    ],
}


def test_sarif_structure() -> None:
    sarif = to_sarif(REPORT)
    assert sarif["version"] == "2.1.0" and sarif["$schema"].endswith("sarif-2.1.0.json")
    semgrep, ruff = sarif["runs"]
    driver = semgrep["tool"]["driver"]
    assert driver["name"] == "semgrep" and driver["version"] == "1.183.0"
    assert [r["id"] for r in driver["rules"]] == ["python-eval-exec", "python-weak-hash"]
    assert driver["rules"][0]["properties"]["security-severity"] == "8.0"
    first, baselined, no_location = semgrep["results"]
    assert first["level"] == "error" and first["baselineState"] == "new"
    location = first["locations"][0]["physicalLocation"]
    assert location["artifactLocation"] == {"uri": "a.py", "uriBaseId": "%SRCROOT%"}
    assert location["region"] == {"startLine": 3}
    assert first["partialFingerprints"]["aiCodingFingerprint/v1"] == "f" * 64
    assert baselined["level"] == "warning" and baselined["baselineState"] == "unchanged"
    assert "region" not in baselined["locations"][0]["physicalLocation"]
    assert "locations" not in no_location
    assert semgrep["automationDetails"]["id"] == "ai-coding/semgrep/"
    assert ruff["invocations"][0]["executionSuccessful"] is False
    assert (
        "image missing"
        in ruff["invocations"][0]["toolExecutionNotifications"][0]["message"]["text"]
    )
    assert json.loads(render_sarif(REPORT)) == sarif


def test_junit_counts_and_elements() -> None:
    root = ET.fromstring(render_junit(REPORT))  # noqa: S314 - our own output
    gates, tools = root.findall("testsuite")
    assert (
        gates.get("tests") == "3" and gates.get("failures") == "1" and gates.get("skipped") == "1"
    )
    failure = gates.find("testcase[@name='security']/failure")
    assert failure is not None and failure.get("type") == "blocking"
    assert "semgrep:eval" in (failure.text or "")
    assert gates.find("testcase[@name='tests']/skipped") is not None
    assert tools.get("errors") == "1"
    assert tools.find("testcase[@name='ruff']/error") is not None
    assert tools.find("testcase[@name='semgrep']").get("time") == "2.5"  # type: ignore[union-attr]


def test_exports_redact_secrets() -> None:
    token = "ghp_" + "z" * 36
    report = json.loads(json.dumps(REPORT))
    report["findings"][0]["message"] = f"leaked {token}"
    report["gates"][0]["reason"] = f"reason {token}"
    assert token not in render_sarif(report) and token not in render_junit(report)
