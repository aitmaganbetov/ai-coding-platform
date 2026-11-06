"""``ai-coding pr open`` — push a work branch and open a PR under policy (FR-17, AC-03)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from ai_coding.cli.commands.run import finish_run
from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator, resolve_run
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.cli.workflow import github_runtime, show_preview, start_or_resume
from ai_coding.orchestrator import RunOptions
from ai_coding.workflows.pull_request import (
    PullRequestRequest,
    build_pull_request,
    pull_request_registry,
)

app = typer.Typer(help="Pull requests (never merged by the platform).", no_args_is_help=True)


@app.callback()
def _group(ctx: typer.Context) -> None:
    """Pull requests (never merged by the platform)."""
    ctx.call_on_close(close_all)


@app.command("open")
def open_pr(
    branch: Annotated[
        str, typer.Option("--branch", "-b", help="Work branch to push (not protected).")
    ],
    title: Annotated[str, typer.Option("--title", "-t")],
    body_file: Annotated[Path | None, typer.Option("--body-file")] = None,
    base: Annotated[
        str | None, typer.Option("--base", help="Default: project.default_branch.")
    ] = None,
    draft: Annotated[bool, typer.Option("--draft")] = False,
    remote: Annotated[str, typer.Option("--remote")] = "origin",
    repo: Annotated[str | None, typer.Option("--repo", "-R")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path(),
    require_approval: Annotated[bool, typer.Option("--require-approval")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Push ``--branch`` and open a PR into the default branch, as an orchestrated run.

    The PR step requires the ``pr_create`` approval when the policy says so; approve
    with ``ai-coding run approve <id>`` and continue with ``ai-coding pr resume <id>``.
    """
    root, config = load_project(project)
    target = base or config.project.default_branch
    if branch == target:
        error("the work branch must differ from the base branch")
        raise typer.Exit(ExitCode.USAGE)
    body = body_file.read_text(encoding="utf-8") if body_file else ""
    host, token, api_host = github_runtime(repo, root)
    request = PullRequestRequest(
        branch=branch, base=target, title=title, body=body, draft=draft, remote=remote
    )
    registry, steps = build_pull_request(request, host, token=token, api_host=api_host)
    orch = open_orchestrator(root, config, registry)
    options = RunOptions(require_approval=require_approval)
    if dry_run:
        show_preview(orch, steps, options)
        info("dry run: nothing was pushed or opened")
        return
    _, status = start_or_resume(
        orch, steps, resume=None, goal=f"pr: {branch} → {target}", options=options
    )
    finish_run(status, as_json=False)


@app.command("resume")
def resume_pr(
    run_id: Annotated[str, typer.Argument(help="Run id (or unique prefix).")],
    repo: Annotated[str | None, typer.Option("--repo", "-R")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path(),
) -> None:
    """Resume a PR run (e.g. after ``ai-coding run approve``) with the PR handlers.

    The generic ``run resume`` only knows the built-in handlers; the branch, base and
    title come from the persisted step inputs, so nothing has to be repeated here.
    """
    root, config = load_project(project)
    host, token, api_host = github_runtime(repo, root)
    orch = open_orchestrator(
        root, config, pull_request_registry(host, token=token, api_host=api_host)
    )
    rid = resolve_run(orch, run_id)
    finish_run(guard(lambda: asyncio.run(orch.resume(rid))), as_json=False)
