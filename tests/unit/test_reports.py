"""Audit report rendering (JSON + Markdown)."""

from __future__ import annotations

import json

from ai_coding.reports import AuditReport, render_markdown
from ai_coding.reports.audit import MAX_MARKDOWN_FINDINGS


def report(findings: list[dict], **kw: object) -> AuditReport:  # type: ignore[type-arg]
    data = {
        "project": "demo",
        "run_id": "r1",
        "generated_at": "2026-11-05T00:00:00+00:00",
        "backend": "docker",
        "verdict": "FAIL",
        "gates": [
            {
                "gate": "security",
                "result": "FAIL",
                "blocking": True,
                "reason": "1 | finding",
                "evidence": [],
            }
        ],
        "tools": [
            {
                "tool": "semgrep",
                "version": "1.183.0",
                "succeeded": True,
                "error": None,
                "findings": findings,
                "duration_seconds": 1.0,
            },
            {
                "tool": "ruff",
                "version": None,
                "succeeded": False,
                "error": "image missing",
                "findings": [],
                "duration_seconds": 0.0,
            },
        ],
        "findings": findings,
        "baseline": {"suppressed": 1, "expired": 0},
        "integrity": {"unchanged": True, "method": "digest"},
        "discovery": {},
    }
    data.update(kw)
    return AuditReport(**data)  # type: ignore[arg-type]


def f(sev: str, status: str = "new", path: str = "a.py", line: int = 1) -> dict:  # type: ignore[type-arg]
    return {
        "tool": "semgrep",
        "rule_id": "r|x",
        "severity": sev,
        "title": "t",
        "message": "m",
        "path": path,
        "line": line,
        "fingerprint": "f" * 64,
        "evidence": {},
        "status": status,
    }


def test_json_report_counts_only_new() -> None:
    r = report([f("high"), f("high", status="baselined"), f("low")])
    data = json.loads(r.to_json())
    assert data["summary"] == {"critical": 0, "high": 1, "medium": 0, "low": 1, "info": 0}
    assert (
        data["schema_version"] == "2"
    )  # v2 (S7): triage, suppressions, reproducibility and data["verdict"] == "FAIL"


def test_markdown_sorted_escaped_and_complete() -> None:
    md = render_markdown(
        report(
            [f("low", path="z.py"), f("high", path="b.py"), f("info", status="baselined")],
            notes=["note | one"],
        )
    )
    assert "**Verdict: ❌ FAIL**" in md
    assert "| security | ❌ FAIL | yes | 1 \\| finding |" in md
    assert md.index("b.py") < md.index("z.py")
    assert "`r\\|x`" in md
    assert "error: image missing" in md
    assert "Suppressed by active baseline entries: 1" in md
    assert "- note \\| one" in md


def test_markdown_truncates_and_handles_empty() -> None:
    many = [f("medium", line=i) for i in range(MAX_MARKDOWN_FINDINGS + 5)]
    assert "5 more in the JSON report" in render_markdown(report(many))
    assert "No new findings." in render_markdown(report([], verdict="PASS"))


def test_reports_are_redacted() -> None:
    token = "ghp_" + "r" * 36
    r = report([f("high")], notes=[f"leaked {token}"])
    assert token not in r.to_json() and token not in render_markdown(r)
