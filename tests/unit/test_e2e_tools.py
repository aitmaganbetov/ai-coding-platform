"""Playwright and k6 adapters (B-603): parsers on real tool output, applicability rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_coding.tools import default_tool_registry
from ai_coding.tools.e2e import (
    PLAYWRIGHT_VERSION,
    K6Adapter,
    PlaywrightAdapter,
    find_k6_scripts,
)
from ai_coding.tools.project import Invocation

TOOLS = Path(__file__).resolve().parents[1] / "fixtures" / "tools"


def _config(root: Path, **tests: object) -> None:
    doc = {"schema_version": "2.1", "project": {"name": "p"},
           "quality_gates": {"tests": tests}}  # fmt: skip
    (root / ".ai-coding.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_playwright_report_parsing() -> None:
    report = (TOOLS / "playwright-report.json").read_text(encoding="utf-8")
    (finding,) = PlaywrightAdapter().parse(report, "", 1)  # real output: 1 failed, 1 passed
    assert finding.path == "tests/ui.spec.js" and finding.line == 8
    assert finding.title == "E2E test failed: shows the total" and "\x1b" not in finding.message
    assert "Expected" in finding.message and finding.evidence["status"] == "failed"
    assert PlaywrightAdapter().parse(report.replace('"failed"', '"passed"'), "", 0) == []
    broken = json.dumps({"suites": [], "errors": [{"message": "config error"}], "stats": {}})
    with pytest.raises(ValueError, match="config error"):
        PlaywrightAdapter().parse(broken, "", 1)
    assert PlaywrightAdapter().parse_version("Version 1.56.1") == "1.56.1"


def test_playwright_applicability(tmp_path: Path) -> None:
    tool = PlaywrightAdapter()
    assert tool.plan(tmp_path) == "no Playwright configuration"
    (tmp_path / "playwright.config.js").write_text("module.exports = {}\n", encoding="utf-8")
    pkg = {"devDependencies": {"@playwright/test": "^1.50.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    assert "not pinned" in str(tool.plan(tmp_path))
    pkg["devDependencies"]["@playwright/test"] = "1.50.0"
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    assert f"browsers of {PLAYWRIGHT_VERSION}" in str(tool.plan(tmp_path))
    pkg["devDependencies"]["@playwright/test"] = PLAYWRIGHT_VERSION
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    plan = tool.plan(tmp_path)
    assert isinstance(plan, Invocation) and plan.install is not None and plan.writable
    assert plan.egress is None  # the browser tests run without network
    _config(tmp_path, e2e=False)
    assert tool.plan(tmp_path) == "disabled by quality_gates.tests.e2e"


def _k6_line(script: str, summary: object, code: int = 0) -> str:
    return json.dumps({"script": script, "exit": code, "summary": summary, "error": ""})


def test_k6_threshold_parsing() -> None:
    summary = json.loads((TOOLS / "k6-summary.json").read_text(encoding="utf-8"))  # real
    out = _k6_line("k6/api.js", summary, 99) + "\n"
    (finding,) = K6Adapter().parse(out, "", 0)  # p(95)<1 crossed; avg and checks held
    assert finding.rule_id == "threshold:work_ms" and finding.path == "k6/api.js"
    assert "p(95)<1" in finding.title and finding.evidence["p(95)"] == 5
    passing = json.loads(json.dumps(summary).replace("true", "false"))
    assert K6Adapter().parse(_k6_line("k6/api.js", passing), "", 0) == []
    with pytest.raises(ValueError, match=r"could not run k6/bad\.js"):
        K6Adapter().parse(_k6_line("k6/bad.js", None, 107), "", 0)
    with pytest.raises(ValueError, match="no results"):
        K6Adapter().parse("", "", 0)
    assert K6Adapter().parse_version("k6 v1.3.0 (go1.25.1)") == "1.3.0"


def test_k6_applicability(tmp_path: Path) -> None:
    tool = K6Adapter()
    assert "no k6 scripts" in str(tool.plan(tmp_path))
    (tmp_path / "k6").mkdir()
    (tmp_path / "k6" / "smoke.js").write_text("export default function () {}\n", encoding="utf-8")
    (tmp_path / "perf").mkdir()
    (tmp_path / "perf" / "api.k6.js").write_text("export default function () {}\n",
                                                 encoding="utf-8")  # fmt: skip
    (tmp_path / "node_modules" / "k6").mkdir(parents=True)
    (tmp_path / "node_modules" / "k6" / "x.k6.js").write_text("", encoding="utf-8")
    assert find_k6_scripts(tmp_path) == ["k6/smoke.js", "perf/api.k6.js"]
    assert "performance_hosts" in str(tool.plan(tmp_path))
    _config(tmp_path, performance_hosts=["staging.internal:8080"])
    plan = tool.plan(tmp_path)
    assert isinstance(plan, Invocation) and plan.egress == ("staging.internal:8080",)
    assert plan.egress_private and plan.argv[-2:] == ("k6/smoke.js", "perf/api.k6.js")
    _config(tmp_path, performance_hosts=["s.internal"], performance_scripts=["k6/missing.js"])
    assert "not found: k6/missing.js" in str(tool.plan(tmp_path))


def test_registered_and_gated_as_tests() -> None:
    from ai_coding.quality.gates import _GATE_CATEGORIES  # noqa: PLC0415
    from ai_coding.tools.base import ToolCategory  # noqa: PLC0415

    names = set(default_tool_registry(plugins=False).names())
    assert {"playwright", "k6"} <= names
    tests_gate = next(c for g, c in _GATE_CATEGORIES.items() if g.value == "tests")
    assert {ToolCategory.TESTS, ToolCategory.PERFORMANCE} <= tests_gate
