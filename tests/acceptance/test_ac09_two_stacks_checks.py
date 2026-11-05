"""AC-09: GitHub Actions connected to two different stacks give check statuses in the PR.

The pilots workflow wires two stacks (Python, Node) to the reusable workflows. This test
reads that wiring and runs exactly what the reusable workflows run — the offline Docker
audit and the publication of a named check run and a keyed PR comment — against a fake
GitHub API, for both stacks on one head commit. The same workflows run for real on every
pull request of this repository (evidence: docs/acceptance.md).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.tools import default_tool_registry
from tests.fake_github import FakeGitHub
from tests.integration.conftest import require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]
runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
HEAD = "c" * 40


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _audit_jobs() -> list[dict[str, Any]]:
    """Pilot jobs that run an ai-coding audit and publish a check, with their inputs."""
    jobs = []
    for key, job in _workflow("pilots.yml")["jobs"].items():
        target = job["uses"].removeprefix("./.github/workflows/")
        spec = _workflow(target)
        trigger = spec.get("on", spec.get(True))  # YAML 1.1 reads the key `on` as True
        inputs = trigger["workflow_call"]["inputs"]
        if "check-name" not in inputs:
            continue  # the plain test workflows (pytest / npm test)
        given = job.get("with", {})
        jobs.append({
            "job": key, "workflow": target, "dir": given["working-directory"],
            "tools": str(given.get("tools", inputs["tools"]["default"])).split(),
            "check": given["check-name"], "key": given["comment-key"],
            "permissions": job.get("permissions", {}),
        })  # fmt: skip
    return jobs


@pytest.mark.acceptance("AC-09")
def test_two_stacks_get_their_checks_in_the_pr(fake_github: FakeGitHub, tmp_path: Path) -> None:
    """Python and Node pilots: two named check runs and two PR comments on one head."""
    jobs = _audit_jobs()
    stacks = {Path(j["dir"]).name for j in jobs}
    assert stacks == {"pilot-python", "pilot-node"}, jobs
    assert len({j["check"] for j in jobs}) == len(jobs)  # distinct statuses in the PR
    assert all(j["permissions"].get("checks") == "write" for j in jobs)
    registry = default_tool_registry()
    require_images(*{registry.get(t).spec.image for j in jobs for t in j["tools"]})

    for _round in range(2):  # a re-run (new push) updates instead of duplicating comments
        for job in jobs:
            work = tmp_path / f"{Path(job['dir']).name}-{_round}"
            skip = shutil.ignore_patterns(".ruff_cache", "node_modules")
            shutil.copytree(REPO / job["dir"], work, ignore=skip)
            for path in [work, *work.rglob("*")]:
                path.chmod(0o755 if path.is_dir() else 0o644)
            out = work.parent / f"out-{job['job']}-{_round}"
            argv = ["audit", "--project", str(work), "--backend", "docker", "--output", str(out)]
            for tool in job["tools"]:
                argv += ["--tool", tool]
            audit = runner.invoke(app, argv)
            assert audit.exit_code == 0, audit.output  # the pilots are clean
            report = str(out / "audit-report.json")
            check = runner.invoke(app, ["github", "check", "--sha", HEAD, "--report", report,
                                        "--name", job["check"]])  # fmt: skip
            assert check.exit_code == 0, check.output
            comment = runner.invoke(app, ["github", "comment", "--pr", "10", "--report", report,
                                          "--key", job["key"]])  # fmt: skip
            assert comment.exit_code == 0, comment.output

    runs = list(fake_github.check_runs.values())
    by_name = {r["name"]: r for r in runs}
    assert set(by_name) == {j["check"] for j in jobs}
    assert all(r["head_sha"] == HEAD and r["conclusion"] == "success" for r in runs)
    assert len(fake_github.comments) == len(jobs)  # one comment per stack, upserted
    bodies = json.dumps([c["body"] for c in fake_github.comments.values()])
    assert all(f"ai-coding:{j['key']}" in bodies for j in jobs)
