"""AC-10: dry-run, manual approval, cancellation and export of the audit trail — the
operator's controls through the CLI, on a mutating operation (opening a PR: push to a
remote and a pull request against a fake GitHub API)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.git import LocalGitRepository
from tests.fake_github import FakeGitHub

pytestmark = pytest.mark.integration
runner = CliRunner()


def cli(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), input=input)
    return result.exit_code, result.output


def runs(root: Path) -> dict[str, str]:
    return {r["id"]: r["state"]
            for r in json.loads(cli("run", "list", "-C", str(root), "--json")[1])}  # fmt: skip


@pytest.mark.acceptance("AC-10")
def test_dry_run_approval_cancel_and_audit_export(
    git_repo: LocalGitRepository, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    """Dry-run executes nothing; a PR waits for a person; cancel works; the trail exports."""
    repo, root = git_repo, git_repo.root
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", bare.as_uri()], cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "ai/feature"], cwd=root, check=True)
    (root / "f.txt").write_text("x", encoding="utf-8")
    repo.commit("feat: f")
    fake_github.head_shas["ai/feature"] = repo.rev_parse("ai/feature")
    assert cli("init", str(root), "--mode", "develop")[0] == 0
    cfg = root / ".ai-coding.yml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
        "  network: local-only       # local-only | allowlist | open\n  egress_allowlist: []",
        "  network: allowlist\n  egress_allowlist: [localhost]",
    ), encoding="utf-8")  # fmt: skip
    c = str(root)
    pr = ["pr", "open", "-C", c, "--branch", "ai/feature", "--title", "Add f"]

    # Dry-run: the plan with its approval point is shown; nothing runs, nothing is stored.
    code, out = cli(*pr, "--dry-run")
    assert code == 0 and "github.pr" in out and "require_approval" in out
    assert runs(root) == {} and fake_github.pulls == {}
    assert repo.remote_branch_sha("ai/feature") is None

    # Manual approval: the run waits; nothing is pushed until a person approves.
    assert cli(*pr)[0] == ExitCode.PENDING
    (first,) = runs(root)
    assert runs(root)[first] == "AWAITING_APPROVAL" and fake_github.pulls == {}
    code, out = cli("run", "approve", first, "-C", c, "--note", "reviewed by me")
    assert code == 0, out
    code, out = cli("pr", "resume", first, "-C", c)
    assert code == ExitCode.OK, out
    assert len(fake_github.pulls) == 1 and runs(root)[first] == "COMPLETED"

    # Cancellation: a second waiting run; declining keeps it, confirming cancels it.
    assert cli(*pr, "--title", "Add f again")[0] == ExitCode.PENDING
    second = next(r for r in runs(root) if r != first)
    assert cli("run", "cancel", second, "-C", c, input="n\n")[0] == ExitCode.ABORTED
    assert runs(root)[second] == "AWAITING_APPROVAL"
    code, out = cli("run", "cancel", second, "-C", c, "--yes", "--reason", "not needed")
    assert code == 0 and "CANCELLED" in out, out
    assert runs(root)[second] == "CANCELLED" and len(fake_github.pulls) == 1

    # Audit trail export: who did what, when and with which outcome, per run.
    for run_id, decisive in ((first, "approval.approve"), (second, "run.cancel")):
        target = tmp_path / f"trail-{run_id[-6:]}.json"
        assert cli("run", "audit", run_id, "-C", c, "-o", str(target))[0] == 0
        trail = json.loads(target.read_text(encoding="utf-8"))
        actions = [e["action"] for e in trail]
        assert actions[0] == "run.create" and decisive in actions, actions
        assert all(e["run_id"] == run_id and e["actor"] and e["outcome"] for e in trail)
    assert fake_github.token not in json.dumps(trail)
