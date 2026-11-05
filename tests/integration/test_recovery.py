"""AC-04 / NFR-03: a crashed worker's run resumes without duplicating commits.

A real worker *process* is killed (``os._exit``) in the middle of a task; a second
worker resumes from the persisted state. The commit is made exactly once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_coding.config.generator import render_initial_config
from ai_coding.domain.enums import RunState, TaskPhase
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator import StepSpec, TaskPlanner
from ai_coding.orchestrator.effects import APPLIED, EffectLedger
from ai_coding.persistence import upgrade_to_head
from ai_coding.persistence.models import Task
from tests.integration.recovery_support import CRASH_ENV, TRAILER, build

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup(git_repo: LocalGitRepository, tmp_path: Path) -> tuple[Path, Path]:
    text, _ = render_initial_config(name="demo")
    (git_repo.root / ".ai-coding.yml").write_text(text.replace("mode: audit", "mode: develop"),
                                                  encoding="utf-8")  # fmt: skip
    worktree = tmp_path / "worktrees" / "task"
    git_repo.add_worktree(worktree, "ai/task-1")
    return git_repo.root, worktree


def _crash_worker(project: Path, run_id: str, crash: str) -> int:
    env = {**os.environ, CRASH_ENV: crash, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "tests.integration.recovery_support", str(project), run_id],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return proc.returncode


@pytest.mark.parametrize("crash", ["after_commit", "before_commit"])
async def test_worker_crash_resumes_without_duplicate_commits(
    git_repo: LocalGitRepository, tmp_path: Path, crash: str
) -> None:
    project, worktree = _setup(git_repo, tmp_path)
    orch = build(project)
    upgrade_to_head(orch.db)
    plan = TaskPlanner().build(
        [StepSpec(key="implement", capability="git.commit", phase=TaskPhase.EXECUTION,
                  input={"worktree": str(worktree)}, max_attempts=3)]
    )  # fmt: skip
    run_id = orch.create_run(plan, goal="AC-04")

    assert _crash_worker(project, str(run_id), crash) == 137
    crashed = orch.status(run_id)
    assert crashed.state is RunState.RUNNING  # nothing marked the run as finished
    assert crashed.tasks[0]["status"] == "RUNNING"
    assert crashed.lease_owner is not None  # the dead worker still "holds" the lease

    time.sleep(1.0)  # let the dead worker's lease expire (ttl 0.3s)
    final = await orch.resume(run_id)
    assert final.state is RunState.COMPLETED
    assert final.tasks[0]["attempts"] == 2

    wt_repo = LocalGitRepository.open(worktree)
    marker = f"{run_id}/implement"
    commits = wt_repo.find_commits_by_trailer(TRAILER, marker)
    assert len(commits) == 1, "the commit must not be duplicated"
    log = subprocess.run(
        ["git", "rev-list", "--count", "main..ai/task-1"], cwd=project,
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert log.stdout.strip() == "1"
    assert EffectLedger(orch.db, run_id, None).status("implement:commit") == APPLIED
    with orch.db.session() as s:
        output = s.query(Task).one().output
    assert output is not None and output["sha"] == commits[0]
    assert output.get("recovered", False) is (crash == "after_commit")
    # Main and the user's working copy are untouched (AC-03).
    assert git_repo.current_branch() == "main"
    assert not (project / "feature.txt").exists()
    events = [e["to"] for e in orch.events(run_id)]
    assert events == ["CREATED", "DISCOVERY", "PLANNED", "RUNNING", "VERIFYING", "COMPLETED"]
    assert any(a["action"] == "run.resume" for a in orch.audit_trail(run_id))
    orch.close()
