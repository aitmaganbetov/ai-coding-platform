"""Helpers shared by the workflow commands (develop, modernize, maintain, create, pr):
GitHub access, the dry-run preview, starting or resuming a run, and the summary of a
committed change."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rich.markup import escape

from ai_coding.cli.commands.github import make_host, resolve_repo
from ai_coding.cli.common import guard, resolve_run
from ai_coding.cli.console import info
from ai_coding.integrations.github import GitHubHost
from ai_coding.integrations.github.client import DEFAULT_API_URL
from ai_coding.orchestrator import Orchestrator, RunOptions, RunStatus, StepSpec, TaskPlanner

GitHubRuntime = tuple[GitHubHost, Callable[[], str | None], str]


def github_runtime(repo: str | None, root: Path) -> GitHubRuntime:
    """The repository's GitHub host, a token provider for ``git push`` and the API host."""
    host = make_host(resolve_repo(repo, root))
    api_host = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).split("://", 1)[-1].split("/")[0]
    return host, host.client.access_token, api_host


def show_preview(
    orch: Orchestrator,
    steps: Sequence[StepSpec],
    options: RunOptions | None = None,
    *,
    verdicts: bool = True,
) -> None:
    """Dry run: each step's handler (or why none), with the policy verdict."""
    for p in orch.preview(TaskPlanner().build(steps), options or RunOptions()):
        line = f"{p.key}: {p.handler or p.route_error}"
        if verdicts:
            line += f" [{p.approval.verdict.value if p.approval else 'allow'}]"
        info(escape(line))


def start_or_resume(
    orch: Orchestrator,
    steps: Sequence[StepSpec],
    *,
    resume: str | None,
    goal: str,
    options: RunOptions | None = None,
) -> tuple[uuid.UUID, RunStatus]:
    """Resume the run ``resume`` names, or plan ``steps`` as a new run and execute it."""
    if resume:
        run_id = resolve_run(orch, resume)
        return run_id, guard(lambda: asyncio.run(orch.resume(run_id)))
    plan = TaskPlanner().build(steps)
    new_id = guard(lambda: orch.create_run(plan, goal=goal[:200], options=options))
    return new_id, guard(lambda: asyncio.run(orch.execute(new_id)))


def report_change(orch: Orchestrator, run_id: Any, step: str) -> None:
    """Summary of a finished implementation step."""
    out = orch.task_output(run_id, step) or {}
    tests = out.get("tests") or {}
    info(f"\n[green]committed[/] on [bold]{escape(str(out.get('branch')))}[/] "
         f"({str(out.get('commit'))[:12]}) after {out.get('iterations')} iteration(s)")  # fmt: skip
    info(f"tests: {escape(str(tests.get('status')))} — {escape(str(tests.get('reason')))}")
    review = out.get("review")
    if review:
        info(f"AI review (advisory): {escape(review['verdict'])}, "
             f"{len(review['findings'])} finding(s)")  # fmt: skip
    pr = orch.task_output(run_id, "github.pr")
    if pr:
        info(f"pull request: {pr.get('url')}")
