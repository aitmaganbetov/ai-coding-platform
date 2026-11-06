"""``ai-coding develop`` — DEVELOP: issue → plan → branch → patch → tests → review → PR
(FR-04, AC-03)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ai_coding.cli.approvals import settle
from ai_coding.cli.commands.run import finish_run
from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.cli.workflow import github_runtime, report_change, show_preview, start_or_resume
from ai_coding.domain.enums import Mode, RunState
from ai_coding.orchestrator import RunOptions
from ai_coding.workflows.develop import DevelopRequest, build_develop
from ai_coding.workflows.implement import ImplementSettings

ProjectOpt = Annotated[Path, typer.Option("--project", "-C", help="Project directory.")]


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
    runtime = github_runtime(repo, root) if needs_github and not dry_run else None
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
        show_preview(orch, steps, options)
        info(
            escape(f"work branch: {request.work_branch} (from {target}); dry run: nothing changed")
        )
        return
    run_id, status = start_or_resume(
        orch, steps, resume=resume, goal=f"develop: {request.title}", options=options
    )
    hint = f"ai-coding develop --resume {run_id} -C {root}" + (" --pr" if pr else "")
    status = settle(orch, run_id, status, interactive=interactive, project=root, resume_hint=hint)
    if status.state is RunState.COMPLETED:
        report_change(orch, run_id, "develop.implement")
    finish_run(status, as_json=False)
