"""AUDIT additions in S7: tool-level suppressions (B-225), Security Agent triage inputs,
report sections (triage, suppressions, reproducibility, skipped tools)."""

from __future__ import annotations

from pathlib import Path

from ai_coding.domain.enums import Severity
from ai_coding.quality.suppressions import SEMGREP_DEFAULT_NOTE, scan_suppressions
from ai_coding.reports import AuditReport, render_markdown
from ai_coding.tools.base import Finding, fingerprint
from ai_coding.workflows.audit import TRIAGE_MAX_FINDINGS, triage_id, triage_inputs


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def finding(tool: str, path: str, sev: Severity = Severity.HIGH, line: int = 1) -> Finding:
    return Finding(tool, f"{tool}-rule", sev, "t", "m", path, line,
                   fingerprint(tool, f"{tool}-rule", path, str(line)))  # fmt: skip


def test_suppressions_are_listed(tmp_path: Path) -> None:
    write(tmp_path, {
        ".semgrepignore": "# comment\nlegacy/\n\nthird_party/\n",
        ".gitleaksignore": "abc123:config/x.php:aws-access-token:2\n",
        "app/a.py": "x = eval(s)  # nosemgrep\nkey = 'k'  # gitleaks:allow\n",
        "web/b.js": "// eslint-disable-next-line\nfoo()\n",
        "node_modules/pkg/c.js": "// nosemgrep\n",  # dependencies are not the project
        "image.png": "nosemgrep",  # not a text source
    })  # fmt: skip
    result = scan_suppressions(tmp_path)
    assert result["files"] == [
        {"path": ".gitleaksignore", "tool": "gitleaks", "entries": 1},
        {"path": ".semgrepignore", "tool": "semgrep", "entries": 2},
    ]
    assert result["inline"] == {"eslint": 1, "gitleaks": 1, "semgrep": 1}
    assert "app/a.py:1 (nosemgrep)" in result["locations"]
    assert result["notes"] == []


def test_semgrep_default_ignore_is_disclosed(tmp_path: Path) -> None:
    result = scan_suppressions(write(tmp_path, {"a.py": "print(1)\n"}))
    assert result == {"files": [], "inline": {}, "locations": [], "notes": [SEMGREP_DEFAULT_NOTE]}


def test_triage_inputs_rank_bound_and_withhold_secret_files(tmp_path: Path) -> None:
    write(tmp_path, {"app/a.php": "<?php\n" + "x\n" * 1000, "config/s.php": "AKIA...\n"})
    findings = [
        finding("semgrep", "app/a.php", Severity.LOW, 3),
        finding("gitleaks", "config/s.php", Severity.HIGH, 1),
        finding("semgrep", "app/a.php", Severity.CRITICAL, 2),
        finding("semgrep", "../outside.php", Severity.MEDIUM),
        *(finding("ruff", f"m{i}.py", Severity.INFO) for i in range(150)),
    ]
    inputs = triage_inputs(tmp_path, findings, ["laravel"])
    assert len(inputs["findings"]) == TRIAGE_MAX_FINDINGS
    assert [f["severity"] for f in inputs["findings"][:2]] == ["critical", "high"]
    assert inputs["findings"][0]["id"] == triage_id(findings[2])
    assert set(inputs["code"]) == {"app/a.php"}  # no secret file, nothing outside the root
    assert len(inputs["code"]["app/a.php"].splitlines()) == 400
    assert inputs["stacks"] == ["laravel"]


def report(**kw: object) -> AuditReport:
    base: dict[str, object] = {
        "project": "p", "run_id": "r", "generated_at": "now", "backend": "docker",
        "verdict": "FAIL", "gates": [], "findings": [], "baseline": {}, "integrity": {},
        "discovery": {},
        "tools": [{"tool": "phpstan", "version": None, "succeeded": False, "skipped": True,
                   "error": "vendor/ is missing", "findings": [], "duration_seconds": 0.0}],
    }  # fmt: skip
    base.update(kw)
    return AuditReport(**base)  # type: ignore[arg-type]


RISK = {"file": "a.php", "line": 2, "severity": "low", "message": "m", "recommendation": "r"}


def test_report_sections() -> None:
    triage = {"status": "done", "triaged": 1, "total": 1, "models": ["ollama/m"],
              "result": {"summary": "One real injection.",
                         "assessments": [{"finding_id": "abc", "verdict": "confirmed",
                                          "severity": "critical", "recommendation": "bind"}],
                         "additional_risks": [RISK]}}  # fmt: skip
    md = render_markdown(report(
        triage=triage,
        suppressions={"files": [{"path": ".semgrepignore", "tool": "semgrep", "entries": 2}],
                      "inline": {"semgrep": 3}, "notes": []},
        reproducibility=[{"tool": "trivy", "version": "0.62.1", "image": "trivy@sha256:abc",
                          "data": {"db_updated_at": "2026-11-04", "db_version": 2}}],
    ))  # fmt: skip
    assert "| phpstan | ? | skipped: vendor/ is missing |" in md
    assert "## Security triage (advisory)" in md and "| `abc` | confirmed | critical |" in md
    assert "do not change the gates" in md and "low: `a.php:2` m" in md
    assert "`.semgrepignore` (semgrep): 2" in md and "inline semgrep markers: 3" in md
    row = "| trivy | 0.62.1 | `trivy@sha256:abc` | db_updated_at=2026-11-04, db_version=2 |"
    assert row in md
    failed = render_markdown(report(triage={"status": "failed", "reason": "no model"}))
    assert "Not available: no model." in failed
    plain = report().to_dict()
    assert plain["schema_version"] == "2" and plain["triage"] is None
    assert "## Security triage" not in render_markdown(report())
