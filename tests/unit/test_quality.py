"""Quality Gates (PASS/FAIL/SKIPPED, fail-closed, strict) and baseline (owner, expiry)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from ai_coding.config.models import QualityGatesConfig
from ai_coding.domain.enums import GateResult, Severity
from ai_coding.quality import (
    Baseline,
    BaselineEntry,
    BaselineError,
    QualityGateEngine,
    load_baseline,
)
from ai_coding.quality.baseline import BASELINE_FILENAME, baseline_from_findings
from ai_coding.quality.gates import GateName
from ai_coding.tools.base import Finding, ToolCategory, ToolRun, fingerprint

TODAY = date(2026, 11, 5)


def finding(tool: str, rule: str, sev: Severity, path: str = "a.py") -> Finding:
    return Finding(tool, rule, sev, rule, "", path, 1, fingerprint(tool, rule, path, rule))


def run(tool: str, cat: ToolCategory, *findings: Finding, ok: bool = True) -> ToolRun:
    return ToolRun(
        tool,
        cat,
        "1.0",
        "docker",
        0 if ok else 2,
        ok,
        tuple(findings),
        0.1,
        None if ok else "crashed",
    )


def gates(report) -> dict[GateName, GateResult]:  # type: ignore[no-untyped-def]
    return {o.gate: o.result for o in report.outcomes}


def evaluate(*runs: ToolRun, baseline: Baseline | None = None, strict: bool = False, **cfg: object):  # type: ignore[no-untyped-def]
    config = QualityGatesConfig.model_validate(cfg)
    return QualityGateEngine(config, strict=strict).evaluate(
        list(runs), baseline or Baseline(), today=TODAY
    )


def test_clean_project_passes_with_honest_skips() -> None:
    report = evaluate(run("gitleaks", ToolCategory.SECRETS), run("ruff", ToolCategory.LINT))
    assert report.verdict is GateResult.PASS
    result = gates(report)
    assert result[GateName.SECURITY] is GateResult.PASS
    assert result[GateName.STATIC] is GateResult.PASS
    skipped = {o.gate: o.reason for o in report.outcomes if o.result is GateResult.SKIPPED}
    assert set(skipped) == {GateName.BUILD, GateName.TESTS, GateName.COVERAGE, GateName.REVIEW}
    assert "no build tool applies" in skipped[GateName.BUILD]


def test_high_finding_fails_security_with_evidence() -> None:
    report = evaluate(
        run(
            "semgrep",
            ToolCategory.SAST,
            finding("semgrep", "eval", Severity.HIGH),
            finding("semgrep", "md5", Severity.MEDIUM),
        )
    )
    security = next(o for o in report.outcomes if o.gate is GateName.SECURITY)
    assert security.result is GateResult.FAIL and report.verdict is GateResult.FAIL
    assert security.evidence == ("semgrep:eval a.py:1 (high)",)


def test_fail_on_threshold() -> None:
    medium = run("semgrep", ToolCategory.SAST, finding("semgrep", "md5", Severity.MEDIUM))
    assert evaluate(medium).verdict is GateResult.PASS
    assert evaluate(medium, security={"fail_on": "medium"}).verdict is GateResult.FAIL


def test_any_new_secret_blocks() -> None:
    secret = run("gitleaks", ToolCategory.SECRETS, finding("gitleaks", "key", Severity.LOW))
    assert evaluate(secret).verdict is GateResult.FAIL
    assert (
        evaluate(secret, security={"block_new_secrets": False, "fail_on": "critical"}).verdict
        is GateResult.PASS
    )


def test_fail_closed_when_tool_breaks() -> None:
    report = evaluate(run("semgrep", ToolCategory.SAST, ok=False))
    security = next(o for o in report.outcomes if o.gate is GateName.SECURITY)
    assert security.result is GateResult.FAIL and "failing closed" in security.reason
    assert evaluate(run("ruff", ToolCategory.LINT, ok=False)).verdict is GateResult.FAIL


def test_static_threshold() -> None:
    lint = run(
        "ruff", ToolCategory.LINT, *(finding("ruff", f"E{i}", Severity.LOW) for i in range(3))
    )
    assert evaluate(lint).verdict is GateResult.FAIL
    assert evaluate(lint, static={"max_errors": 3}).verdict is GateResult.PASS


def test_non_blocking_gate_does_not_fail_verdict() -> None:
    lint = run("ruff", ToolCategory.LINT, finding("ruff", "E1", Severity.LOW))
    report = evaluate(lint, static={"blocking": False})
    assert gates(report)[GateName.STATIC] is GateResult.FAIL and report.verdict is GateResult.PASS


def test_disabled_gates_and_strict_mode() -> None:
    off = {
        g: {"enabled": False}
        for g in ("build", "tests", "static", "security", "coverage", "review")
    }
    report = evaluate(strict=True, **off)
    assert all(o.reason == "gate disabled in configuration" for o in report.outcomes)
    assert report.verdict is GateResult.PASS  # disabled ≠ skipped for strict mode
    strict = evaluate(run("gitleaks", ToolCategory.SECRETS), strict=True)
    assert strict.verdict is GateResult.FAIL and "strict mode" in strict.notes[0]
    assert evaluate().outcomes[3].reason == "no security tool ran"


def test_tool_gates_for_build_and_tests() -> None:
    assert gates(evaluate(run("pytest", ToolCategory.TESTS)))[GateName.TESTS] is GateResult.PASS
    failing = run("pytest", ToolCategory.TESTS, finding("pytest", "test_x", Severity.HIGH))
    assert gates(evaluate(failing))[GateName.TESTS] is GateResult.FAIL
    assert (
        gates(evaluate(run("build", ToolCategory.BUILD, ok=False)))[GateName.BUILD]
        is GateResult.FAIL
    )


def test_evidence_is_bounded() -> None:
    many = run(
        "semgrep",
        ToolCategory.SAST,
        *(finding("semgrep", f"r{i}", Severity.HIGH) for i in range(60)),
    )
    security = next(o for o in evaluate(many).outcomes if o.gate is GateName.SECURITY)
    assert len(security.evidence) == 51 and security.evidence[-1] == "… and 10 more"


# ---------------------------------------------------------------- baseline --
def entry(f: Finding, expires: date, **kw: str) -> BaselineEntry:
    return BaselineEntry(
        fingerprint=f.fingerprint,
        tool=f.tool,
        rule_id=f.rule_id,
        path=f.path,
        owner=kw.get("owner", "team-a"),
        reason="legacy code",
        expires_at=expires,
    )


def test_baseline_suppresses_until_expiry() -> None:
    old = finding("semgrep", "eval", Severity.HIGH, "legacy.py")
    fresh = finding("semgrep", "eval", Severity.HIGH, "new.py")
    sast = run("semgrep", ToolCategory.SAST, old, fresh)
    active = Baseline(entries=(entry(old, TODAY + timedelta(days=1)),))
    report = evaluate(sast, baseline=active)
    assert [f.path for f in report.new_findings] == ["new.py"] and len(report.suppressed) == 1
    only_old = run("semgrep", ToolCategory.SAST, old)
    assert evaluate(only_old, baseline=active).verdict is GateResult.PASS
    expired = Baseline(entries=(entry(old, TODAY - timedelta(days=1)),))
    report = evaluate(only_old, baseline=expired)
    assert report.verdict is GateResult.FAIL and len(report.expired_baseline) == 1
    assert "expired baseline" in report.notes[0]


def test_baseline_entry_requires_owner_reason_expiry() -> None:
    with pytest.raises(ValueError):
        BaselineEntry(
            fingerprint="a" * 64, tool="t", rule_id="r", owner="", reason="ok!", expires_at=TODAY
        )
    with pytest.raises(ValueError):
        BaselineEntry.model_validate(
            {"fingerprint": "a" * 64, "tool": "t", "rule_id": "r", "owner": "o", "reason": "why"}
        )
    with pytest.raises(ValueError):
        BaselineEntry(
            fingerprint="not-hex", tool="t", rule_id="r", owner="o", reason="why", expires_at=TODAY
        )


def test_baseline_file_roundtrip(tmp_path: Path) -> None:
    assert load_baseline(tmp_path) == Baseline()
    findings = [
        finding("ruff", "E1", Severity.LOW, "b.py"),
        finding("ruff", "E1", Severity.LOW, "a.py"),
    ]
    baseline = baseline_from_findings(
        findings + findings[:1], owner="me", reason="adopt", expires_at=TODAY
    )
    assert len(baseline.entries) == 2
    (tmp_path / BASELINE_FILENAME).write_text(baseline.to_json(), encoding="utf-8")
    loaded = load_baseline(tmp_path)
    assert [e.path for e in loaded.entries] == ["a.py", "b.py"]  # sorted, review-friendly
    (tmp_path / BASELINE_FILENAME).write_text(
        '{"version": 1, "entries": [{"x": 1}]}', encoding="utf-8"
    )
    with pytest.raises(BaselineError, match="invalid"):
        load_baseline(tmp_path)


def test_skipped_tools_do_not_fail_gates_but_explain_the_skip() -> None:
    phpstan = replace(run("phpstan", ToolCategory.TYPES, ok=False), skipped=True,
                      error="vendor/ is missing")  # fmt: skip
    report = evaluate(run("gitleaks", ToolCategory.SECRETS), phpstan)
    reasons = {o.gate: o.reason for o in report.outcomes}
    assert gates(report)[GateName.STATIC] is GateResult.SKIPPED
    assert "phpstan: vendor/ is missing" in reasons[GateName.STATIC]
    assert report.verdict is GateResult.PASS
    strict = evaluate(run("gitleaks", ToolCategory.SECRETS), phpstan, strict=True)
    assert strict.verdict is GateResult.FAIL  # strict: a blocking SKIPPED gate fails


def test_coverage_gate_uses_test_tool_coverage() -> None:
    low = replace(run("go-test", ToolCategory.TESTS), coverage=61.5)
    high = replace(run("cargo-test", ToolCategory.TESTS), coverage=92.0)
    report = evaluate(high, low, coverage={"min_percent": 70})
    outcome = next(o for o in report.outcomes if o.gate is GateName.COVERAGE)
    assert outcome.result is GateResult.FAIL and "61.5% < 70.0%" in outcome.reason
    assert report.verdict is GateResult.FAIL
    ok = evaluate(high, coverage={"min_percent": 70})
    assert gates(ok)[GateName.COVERAGE] is GateResult.PASS
    none = evaluate(run("go-test", ToolCategory.TESTS))
    assert gates(none)[GateName.COVERAGE] is GateResult.SKIPPED
