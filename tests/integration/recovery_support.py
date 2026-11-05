"""Support code for the crash/recovery acceptance test (AC-04). Test-only."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from ai_coding.config.validator import validate_file
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator import HandlerRegistry, Orchestrator, StepContext, StepResult
from ai_coding.persistence import Database

TRAILER = "AI-Coding-Task"
CRASH_ENV = "AI_CODING_TEST_CRASH"  # "after_commit" | "before_commit" | ""


class CommitHandler:
    """Writes a file in the agent worktree and commits it exactly once per task."""

    name = "test.commit"
    capabilities = frozenset({"git.commit"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        worktree = Path(str(ctx.input["worktree"]))
        repo = LocalGitRepository.open(worktree)
        marker = f"{ctx.run_id}/{ctx.task_key}"
        crash = os.environ.get(CRASH_ENV, "")
        if crash == "before_commit" and ctx.attempt == 1:
            os._exit(137)  # simulated kill -9 before any effect

        def commit() -> dict[str, Any]:
            (worktree / "feature.txt").write_text(f"change by {marker}\n", encoding="utf-8")
            sha = repo.commit("feat: automated change", trailers={TRAILER: marker})
            if crash == "after_commit" and ctx.attempt == 1:
                os._exit(137)  # simulated kill -9 after the commit, before the ledger update
            return {"sha": sha}

        def probe() -> dict[str, Any] | None:
            found = repo.find_commits_by_trailer(TRAILER, marker)
            return {"sha": found[0], "recovered": True} if found else None

        result = ctx.effect("commit", commit, probe=probe)
        return StepResult(output=result, evidence_refs=[f"git:{result['sha']}"])


def build(project: Path, *, lease_ttl: float = 0.3) -> Orchestrator:
    report = validate_file(project / ".ai-coding.yml")
    assert report.config is not None
    return Orchestrator(
        db=Database.for_project(project),
        config=report.config,
        project_root=project,
        registry=HandlerRegistry([CommitHandler()]),
        actor="local:tester",
        lease_ttl=lease_ttl,
        poll_interval=0.01,
    )


def main() -> None:  # entry point of the crashing worker process
    project, run_id = Path(sys.argv[1]), uuid.UUID(sys.argv[2])
    orch = build(project)
    asyncio.run(orch.execute(run_id))
    orch.close()


if __name__ == "__main__":
    main()
