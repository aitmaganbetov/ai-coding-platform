"""S3 acceptance: safe local AUDIT (ТЗ §16) — read-only, offline, deterministic gates."""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config import validate_file
from ai_coding.domain.enums import ExecutionBackend, RunState, TaskPhase
from ai_coding.orchestrator import HandlerRegistry, StepContext, StepResult, StepSpec, TaskPlanner
from ai_coding.persistence import Database, upgrade_to_head
from ai_coding.persistence.models import Finding as FindingRow
from ai_coding.tools.gitleaks import GITLEAKS_IMAGE
from ai_coding.tools.semgrep import SEMGREP_IMAGE, bundled_rules_dir
from ai_coding.workflows import AuditOptions, build_audit
from ai_coding.workflows.audit import IntegrityHandler, SnapshotHandler, snapshot_digest
from tests.helpers import make_orchestrator
from tests.integration.conftest import require_images

pytestmark = pytest.mark.integration
runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AWS_KEY = "AKIA" + "QYLPMN5HHHFPZAM2"


@pytest.fixture
def vulnerable(tmp_path: Path) -> Path:
    """A small project with a lint issue, an insecure call and a committed credential."""
    root = tmp_path / "vuln"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text(
        "import os\nimport subprocess\n\n\ndef run(cmd):\n"
        "    subprocess.run(cmd, shell=True)\n    return eval(cmd)\n",
        encoding="utf-8",
    )
    (root / "app" / "settings.py").write_text(
        f'AWS_ACCESS_KEY_ID = "{AWS_KEY}"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'vuln'\n", encoding="utf-8")
    assert runner.invoke(app, ["init", str(root)]).exit_code == 0
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    return root


def _require_local(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        pytest.skip(f"not on PATH: {', '.join(missing)}")


def test_local_audit_lint_only(vulnerable: Path) -> None:
    _require_local("ruff")
    out = vulnerable.parent / "reports"
    result = runner.invoke(
        app,
        ["audit", "-C", str(vulnerable), "--backend", "local", "--tool", "ruff", "-o", str(out)],
    )
    assert result.exit_code == ExitCode.INVALID, result.output
    report = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    gates = {g["gate"]: g["result"] for g in report["gates"]}
    assert gates["static"] == "FAIL" and gates["security"] == "SKIPPED"
    assert report["integrity"]["unchanged"] is True
    rules = {f["rule_id"] for f in report["findings"]}
    assert "F401" in rules  # unused import os
    assert (out / "audit-report.md").read_text(encoding="utf-8").startswith("# Audit report — vuln")
    assert report["tools"][0]["version"]


def test_baseline_adoption_flow(vulnerable: Path) -> None:
    """Existing problems can be accepted with owner/expiry; new ones still fail (ТЗ §10)."""
    _require_local("ruff")
    out = vulnerable.parent / "r1"
    first = runner.invoke(
        app, ["audit", "-C", str(vulnerable), "-b", "local", "-t", "ruff", "-o", str(out)]
    )
    assert first.exit_code == ExitCode.INVALID
    expires = (date.today() + timedelta(days=90)).isoformat()
    baseline = vulnerable / ".ai-coding-baseline.json"
    created = runner.invoke(app, ["baseline", "create", str(out / "audit-report.json"), "--owner",
                                  "team-core", "--reason", "legacy code, tracked in JIRA-1",
                                  "--expires", expires, "-o", str(baseline)])  # fmt: skip
    assert created.exit_code == 0, created.output
    assert json.loads(baseline.read_text(encoding="utf-8"))["entries"][0]["owner"] == "team-core"
    second = runner.invoke(
        app, ["audit", "-C", str(vulnerable), "-b", "local", "-t", "ruff", "--json"]
    )
    assert second.exit_code == 0, second.output
    report = json.loads(second.output)
    assert report["verdict"] == "PASS" and report["baseline"]["suppressed"] >= 1
    (vulnerable / "app" / "new.py").write_text("import sys\n", encoding="utf-8")
    third = runner.invoke(
        app, ["audit", "-C", str(vulnerable), "-b", "local", "-t", "ruff", "--json"]
    )
    assert third.exit_code == ExitCode.INVALID
    assert any(
        f["path"] == "app/new.py" and f["status"] == "new"
        for f in json.loads(third.output)["findings"]
    )


def test_baseline_create_errors(tmp_path: Path) -> None:
    report = tmp_path / "r.json"
    report.write_text('{"findings": []}', encoding="utf-8")
    future = (date.today() + timedelta(days=1)).isoformat()
    base = ["baseline", "create", str(report), "--owner", "o", "--reason", "why"]
    assert runner.invoke(app, [*base, "--expires", "2020-01-01"]).exit_code == ExitCode.USAGE
    assert runner.invoke(app, [*base, "--expires", "not-a-date"]).exit_code == ExitCode.USAGE
    out = tmp_path / "b.json"
    assert runner.invoke(app, [*base, "--expires", future, "-o", str(out)]).exit_code == 0
    assert (
        runner.invoke(app, [*base, "--expires", future, "-o", str(out)]).exit_code
        == ExitCode.INVALID
    )
    missing = ["baseline", "create", str(tmp_path / "nope.json"), "--owner", "o", "--reason", "why",
               "--expires", future, "-o", str(tmp_path / "c.json")]  # fmt: skip
    assert runner.invoke(app, missing).exit_code == ExitCode.NOT_FOUND
    report.write_text('{"nope": 1}', encoding="utf-8")
    assert (
        runner.invoke(app, [*base, "--expires", future, "-o", str(tmp_path / "d.json")]).exit_code
        == ExitCode.INVALID
    )


def test_audit_cli_errors(vulnerable: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: str) -> int:
        return runner.invoke(app, ["audit", "-C", str(vulnerable), *args]).exit_code

    assert run("--backend", "github-actions") == ExitCode.USAGE
    assert run("--semgrep-config", "p/python") == ExitCode.USAGE
    assert run("--tool", "nope") == ExitCode.USAGE
    dry = runner.invoke(app, ["audit", "-C", str(vulnerable), "--dry-run", "-b", "docker"])
    assert (
        dry.exit_code == 0 and "scan.semgrep" in dry.output and "nothing was executed" in dry.output
    )
    monkeypatch.setattr("ai_coding.cli.commands.audit.docker_available", lambda: False)
    assert run("--backend", "docker") == ExitCode.NOT_FOUND


async def test_integrity_violation_fails_run(tmp_path: Path) -> None:
    """A scan that modifies the repository makes the audit fail (FR-06 read-only)."""

    class Tamper:
        name = "tamper"
        capabilities = frozenset({"scan.tamper"})
        cost_usd = 0.0

        def is_available(self) -> bool:
            return True

        async def run(self, ctx: StepContext) -> StepResult:
            (ctx.project_root / "planted.txt").write_text("x", encoding="utf-8")
            return StepResult()

    orch = make_orchestrator(tmp_path, [SnapshotHandler(), Tamper(), IntegrityHandler()])
    plan = TaskPlanner().build([
        StepSpec(key="audit.snapshot", capability="audit.snapshot", phase=TaskPhase.DISCOVERY),
        StepSpec(key="scan.tamper", capability="scan.tamper", depends_on=("audit.snapshot",)),
        StepSpec(key="audit.integrity", capability="audit.integrity", phase=TaskPhase.VERIFICATION,
                 depends_on=("audit.snapshot", "scan.tamper")),
    ])  # fmt: skip
    status = await orch.execute(orch.create_run(plan))
    assert status.state is RunState.FAILED
    assert "changed during a read-only audit" in (status.error or "")


def test_snapshot_digest_ignores_state_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    digest, files = snapshot_digest(tmp_path)
    (tmp_path / ".ai-coding").mkdir()
    (tmp_path / ".ai-coding" / "state.db").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("x", encoding="utf-8")
    assert snapshot_digest(tmp_path) == (digest, files) and files == 1
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    assert snapshot_digest(tmp_path)[0] != digest


# ------------------------------------------------------------------ docker --
@pytest.mark.docker
def test_docker_audit_finds_planted_issues_offline(vulnerable: Path) -> None:
    """AC-02/AC-05 shape: sandboxed, network-less audit with evidence and paths."""
    require_images(GITLEAKS_IMAGE, SEMGREP_IMAGE)
    result = runner.invoke(app, ["audit", "-C", str(vulnerable), "-b", "docker", "-t", "gitleaks",
                                 "-t", "semgrep", "--json"])  # fmt: skip
    assert result.exit_code == ExitCode.INVALID, result.output
    report = json.loads(result.output)
    assert report["backend"] == "docker" and report["verdict"] == "FAIL"
    found = {(f["tool"], f["rule_id"], f["path"]) for f in report["findings"]}
    assert ("gitleaks", "aws-access-token", "app/settings.py") in found
    assert ("semgrep", "python-subprocess-shell-true", "app/main.py") in found
    assert ("semgrep", "python-eval-exec", "app/main.py") in found
    assert AWS_KEY not in result.output  # the secret itself never reaches the report
    assert report["integrity"]["unchanged"] is True
    assert {t["tool"]: t["version"] for t in report["tools"]} == {
        "gitleaks": "8.30.1",
        "semgrep": "1.183.0",
    }
    db = Database.for_project(vulnerable)
    upgrade_to_head(db)
    with db.session() as s:
        assert {r.source for r in s.query(FindingRow)} == {"gitleaks", "semgrep"}
    db.dispose()


@pytest.mark.docker
async def test_every_bundled_semgrep_rule_fires(tmp_path: Path) -> None:
    """The offline ruleset is valid and each rule detects its positive sample."""
    require_images(SEMGREP_IMAGE)
    from ai_coding.executors import DockerSandboxExecutor  # noqa: PLC0415
    from ai_coding.tools import ToolRunner, default_tool_registry  # noqa: PLC0415

    run = await ToolRunner(DockerSandboxExecutor(), FIXTURES / "semgrep").run(
        default_tool_registry().get("semgrep")
    )
    assert run.succeeded, run.error
    ruleset = (bundled_rules_dir() / "ai-coding-baseline.yml").read_text(encoding="utf-8")
    rules = {r["id"] for r in yaml.safe_load(ruleset)["rules"]}
    assert {f.rule_id for f in run.findings} == rules


def test_build_audit_selects_tools_by_stack(vulnerable: Path) -> None:
    config = validate_file(vulnerable / ".ai-coding.yml").config
    assert config is not None
    registry, steps, adapters = build_audit(
        config, vulnerable, AuditOptions(ExecutionBackend.LOCAL)
    )
    assert [a.spec.name for a in adapters] == [
        "gitleaks",
        "k6",  # every stack: SKIPPED until scripts and performance_hosts exist
        "mypy",
        "osv-scanner",
        "ruff",
        "semgrep",
        "trivy",
    ]
    keys = [s.key for s in steps]
    assert keys[:4] == ["discover.config", "discover.git", "discover.stacks", "audit.snapshot"]
    assert keys[-3:] == ["quality.gates", "audit.integrity", "report.audit"]
    TaskPlanner().build(steps)
    assert isinstance(registry, HandlerRegistry)
    with pytest.raises(Exception, match="S4"):
        build_audit(config, vulnerable, AuditOptions(ExecutionBackend.GITHUB_ACTIONS))
