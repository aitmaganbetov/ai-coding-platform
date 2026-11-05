"""AC-06: a vulnerable PR does not pass the Quality Gate, and a person sees why and the
evidence — in the report, in the check run (annotations on lines) and in the PR comment.

The clean base branch passes the same gates, so the failure is caused by the PR's change.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.git import LocalGitRepository
from ai_coding.tools.gitleaks import GITLEAKS_IMAGE
from ai_coding.tools.semgrep import SEMGREP_IMAGE
from tests.fake_github import FakeGitHub
from tests.integration.conftest import require_images
from tests.integration.test_audit import AWS_KEY

pytestmark = [pytest.mark.integration, pytest.mark.docker]
runner = CliRunner()


def _audit(root: Path, out: Path) -> tuple[int, dict]:  # type: ignore[type-arg]
    result = runner.invoke(app, ["audit", "-C", str(root), "-b", "docker", "-t", "gitleaks",
                                 "-t", "semgrep", "-o", str(out)])  # fmt: skip
    return result.exit_code, json.loads((out / "audit-report.json").read_text(encoding="utf-8"))


def _readable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if ".git" not in path.parts:
            path.chmod(0o755 if path.is_dir() else 0o644)


@pytest.mark.acceptance("AC-06")
def test_vulnerable_pr_fails_the_gate_with_reasons_and_evidence(
    git_repo: LocalGitRepository, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    """Base passes; the PR adds an injection and a key; check, annotations, comment say why."""
    require_images(GITLEAKS_IMAGE, SEMGREP_IMAGE)
    repo, root = git_repo, git_repo.root
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text(
        "import subprocess\n\n\ndef run(args):\n    return subprocess.run(args, check=True)\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["init", str(root)]).exit_code == 0
    # The base branch is committed by a person (the platform never commits to main).
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: safe runner"], cwd=root, check=True)
    _readable(root)
    base_code, base = _audit(root, tmp_path / "base")
    assert base_code == 0 and base["verdict"] == "PASS", base["gates"]

    # The pull request: shell injection and a committed cloud key.
    subprocess.run(["git", "checkout", "-q", "-b", "feature/report"], cwd=root, check=True)
    (root / "app" / "main.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n",
        encoding="utf-8",
    )
    (root / "app" / "settings.py").write_text(f'AWS_ACCESS_KEY_ID = "{AWS_KEY}"\n',
                                              encoding="utf-8")  # fmt: skip
    head = repo.commit("feat: run reports")
    _readable(root)
    code, report = _audit(root, tmp_path / "pr")

    # The gate fails, with the reason and the evidence in the report.
    assert code == ExitCode.INVALID and report["verdict"] == "FAIL"
    security = next(g for g in report["gates"] if g["gate"] == "security")
    assert security["result"] == "FAIL" and security["blocking"] and security["reason"]
    found = {(f["tool"], f["path"], f["line"]) for f in report["findings"]}
    assert ("semgrep", "app/main.py", 5) in found
    assert ("gitleaks", "app/settings.py", 1) in found
    assert all(f["message"] and f["severity"] for f in report["findings"])

    # The person sees it on the PR: a failing check with line annotations and a comment.
    pr_report = str(tmp_path / "pr" / "audit-report.json")
    check = runner.invoke(app, ["github", "check", "--sha", head, "--report", pr_report])
    assert check.exit_code == 0, check.output
    run = next(iter(fake_github.check_runs.values()))
    assert run["conclusion"] == "failure" and run["head_sha"] == head
    notes = {(a["path"], a["start_line"]) for a in run["output"]["annotations"]}
    assert {("app/main.py", 5), ("app/settings.py", 1)} <= notes
    assert all(a["annotation_level"] == "failure" for a in run["output"]["annotations"]
               if a["path"] == "app/main.py")  # fmt: skip
    comment = runner.invoke(app, ["github", "comment", "--pr", "7", "--report", pr_report])
    assert comment.exit_code == 0, comment.output
    body = next(iter(fake_github.comments.values()))["body"]
    assert "FAIL" in body and "security" in body and security["reason"] in body
    assert "app/main.py" in body and "app/settings.py" in body

    # The evidence never republishes the secret.
    published = json.dumps(fake_github.check_runs) + body + json.dumps(report)
    assert AWS_KEY not in published
