"""AC-02 (S7): connect an existing Laravel+React project, run a read-only audit and get
confirmed findings with file paths — offline, in the sandbox, reproducibly."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.executors import DockerSandboxExecutor
from ai_coding.tools.gitleaks import GITLEAKS_IMAGE
from ai_coding.tools.osv import OSV_IMAGE, OSVScannerAdapter
from ai_coding.tools.php import COMPOSER_IMAGE
from ai_coding.tools.prepare import is_prepared
from ai_coding.tools.semgrep import SEMGREP_IMAGE
from ai_coding.tools.trivy import TRIVY_IMAGE, TrivyAdapter
from ai_coding.workflows.audit import snapshot_digest
from tests.integration.conftest import require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]
runner = CliRunner()
PILOT = Path(__file__).resolve().parents[1] / "fixtures" / "pilot-laravel-react"
AWS_KEY = "".join(("AKIA", "QYLPMN5HHHFPZAM2"))  # planted at run time, never committed


@pytest.fixture
def pilot(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    shutil.copytree(PILOT, root)
    (root / "config" / "services.php").write_text(
        f"<?php\n\nreturn ['ses' => ['key' => '{AWS_KEY}', 'region' => 'eu-west-1']];\n",
        encoding="utf-8",
    )
    init = runner.invoke(app, ["init", str(root)])
    assert init.exit_code == 0, init.output
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return root


def audit(root: Path, out: Path) -> tuple[int, dict]:  # type: ignore[type-arg]
    result = runner.invoke(app, ["audit", "-C", str(root), "-b", "docker", "-o", str(out),
                                 "--json"])  # fmt: skip
    return result.exit_code, json.loads(result.output)


@pytest.mark.acceptance("AC-02")
def test_laravel_react_pilot_audit(pilot: Path, tmp_path: Path) -> None:
    require_images(GITLEAKS_IMAGE, SEMGREP_IMAGE, TRIVY_IMAGE, COMPOSER_IMAGE)
    with_osv = DockerSandboxExecutor().image_present(OSV_IMAGE)
    before = snapshot_digest(pilot)
    code, report = audit(pilot, tmp_path / "r1")
    assert code == ExitCode.INVALID, report
    assert report["verdict"] == "FAIL" and report["backend"] == "docker"
    assert {"laravel", "react"} <= set(report["discovery"]["discover.stacks"]["effective"])

    found = {(f["tool"], f["rule_id"], f["path"]) for f in report["findings"]}
    assert ("gitleaks", "aws-access-token", "config/services.php") in found
    php = "app/Http/Controllers/ReportController.php"
    assert ("semgrep", "laravel-raw-sql-concat", php) in found
    assert ("semgrep", "php-unserialize", php) in found
    assert ("semgrep", "react-dangerously-set-inner-html",
            "resources/js/components/Comment.jsx") in found  # fmt: skip
    tools = {t["tool"]: t for t in report["tools"]}
    if is_prepared(TrivyAdapter()):
        assert ("trivy", "CVE-2022-24775", "composer.lock") in found  # guzzlehttp/psr7
        assert ("trivy", "CVE-2020-8203", "package-lock.json") in found  # lodash
        assert any(t == "trivy" and p == "Dockerfile" for t, _, p in found)  # runs as root
        assert tools["trivy"]["metadata"]["db_updated_at"]
    if with_osv and is_prepared(OSVScannerAdapter()):
        osv = {(r, p) for t, r, p in found if t == "osv-scanner"}
        assert {p for _, p in osv} >= {"composer.lock", "package-lock.json"}

    # Project tools that need dependencies are SKIPPED with the reason, not faked.
    for name in ("phpstan", "pint"):
        assert tools[name]["skipped"] and "network is 'none'" in tools[name]["error"]
    assert tools["eslint"]["skipped"] and "no ESLint configuration" in tools["eslint"]["error"]

    # Every finding has a file path; the secret value never reaches the report.
    assert all(f["path"] for f in report["findings"])
    assert AWS_KEY not in json.dumps(report)
    assert AWS_KEY not in (tmp_path / "r1" / "audit-report.md").read_text(encoding="utf-8")
    md = (tmp_path / "r1" / "audit-report.md").read_text(encoding="utf-8")
    assert "## Reproducibility" in md and "## Tool-level suppressions" in md

    # Read-only: the repository is unchanged (FR-06).
    assert report["integrity"]["unchanged"] is True and snapshot_digest(pilot) == before

    # Reproducible: a second audit yields the same findings.
    _, again = audit(pilot, tmp_path / "r2")
    assert sorted(f["fingerprint"] for f in again["findings"]) == sorted(
        f["fingerprint"] for f in report["findings"]
    )
    assert [r["version"] for r in again["reproducibility"]] == [
        r["version"] for r in report["reproducibility"]
    ]
