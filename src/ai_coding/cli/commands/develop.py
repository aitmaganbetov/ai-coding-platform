"""``ai-coding develop`` — DEVELOP: issue → plan → branch → patch → tests → review → PR
(FR-04, AC-03)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from ai_coding.cli.approvals import settle
from ai_coding.cli.commands.pr import _github_runtime
from ai_coding.cli.commands.run import _finish, _resolve
from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import Mode, RunState
from ai_coding.orchestrator import Orchestrator, RunOptions, TaskPlanner
from ai_coding.workflows.develop import DevelopRequest, build_develop
from ai_coding.workflows.implement import ImplementSettings

ProjectOpt = Annotated[Path, typer.Option("--project", "-C", help="Project directory.")]


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


def develop(
    ctx: typer.Context,
    task: Annotated[
        str | None, typer.Argument(help="What to implement (first line = title).")
    ] = None,
    issue: Annotated[int | None, typer.Option("--issue", "-i", help="GitHub issue number.")] = None,
    task_file: Annotated[
        Path | None, typer.Option("--task-file", help="Read the task from a file.")
    ] = None,
    project: ProjectOpt = Path(),
    base: Annotated[
        str | None, typer.Option("--base", help="Default: project.default_branch.")
    ] = None,
    branch: Annotated[
        str | None, typer.Option("--branch", help="Work branch (default: ai/<n>-<slug>).")
    ] = None,
    file: Annotated[
        list[str] | None,
        typer.Option("--file", help="File to include in the context (repeatable)."),
    ] = None,
    max_iterations: Annotated[int, typer.Option("--max-iterations", min=1, max=10)] = 3,
    strict: Annotated[
        bool, typer.Option("--strict", help="SKIPPED tests are not acceptable.")
    ] = False,
    review: Annotated[
        bool, typer.Option("--review/--no-review", help="AI review each iteration.")
    ] = True,
    pr: Annotated[bool, typer.Option("--pr", help="Push the branch and open a PR.")] = False,
    repo: Annotated[str | None, typer.Option("--repo", "-R", help="owner/repo")] = None,
    resume: Annotated[str | None, typer.Option("--resume", help="Continue a waiting run.")] = None,
    interactive: Annotated[bool | None, typer.Option("--interactive/--no-interactive")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan; change nothing.")
    ] = False,
    require_approval: Annotated[bool, typer.Option("--require-approval")] = False,
) -> None:
    """Implement a task or GitHub issue on a work branch, with sandboxed tests and review."""
    ctx.call_on_close(close_all)
    root, config = load_project(project)
    config = config.model_copy(update={"mode": Mode.DEVELOP})
    interactive = sys.stdin.isatty() if interactive is None else interactive
    target = base or config.project.default_branch
    needs_github = pr or issue is not None
    runtime = _github_runtime(repo, root) if needs_github and not dry_run else None
    title, body = "", ""
    if issue is not None and runtime is not None:
        found = guard(lambda: runtime[0].get_issue(issue))
        title, body = found.title, found.body
    elif task_file is not None or task:
        text = (task_file.read_text(encoding="utf-8") if task_file else task or "").strip()
        title, _, body = text.partition("\n")
    if not resume and not title.strip() and not (dry_run and issue is not None):
        error("describe the task: TASK argument, --task-file or --issue")
        raise typer.Exit(ExitCode.USAGE)
    request = DevelopRequest(
        title=title.strip() or f"issue #{issue}", body=body.strip(), issue_number=issue,
        branch=branch or "", base=target, hint_files=tuple(file or ()),
        settings=ImplementSettings(max_iterations=max_iterations, strict=strict, review=review),
    )  # fmt: skip
    registry, steps = build_develop(request, pull_request=runtime if pr else None)
    orch = open_orchestrator(root, config, registry)
    options = RunOptions(require_approval=require_approval)
    if dry_run:
        for p in orch.preview(TaskPlanner().build(steps), options):
            verdict = p.approval.verdict.value if p.approval else "allow"
            info(escape(f"{p.key}: {p.handler or p.route_error} [{verdict}]"))
        info(
            escape(f"work branch: {request.work_branch} (from {target}); dry run: nothing changed")
        )
        return
    if resume:
        run_id = _resolve(orch, resume)
        status = guard(lambda: asyncio.run(orch.resume(run_id)))
    else:
        plan = TaskPlanner().build(steps)
        goal = f"develop: {request.title}"[:200]
        run_id = guard(lambda: orch.create_run(plan, goal=goal, options=options))
        status = guard(lambda: asyncio.run(orch.execute(run_id)))
    hint = f"ai-coding develop --resume {run_id} -C {root}" + (" --pr" if pr else "")
    status = settle(orch, run_id, status, interactive=interactive, project=root, resume_hint=hint)
    if status.state is RunState.COMPLETED:
        report_change(orch, run_id, "develop.implement")
    _finish(status, as_json=False)
