"""``ai-coding pr open`` — push a work branch and open a PR under policy (FR-17, AC-03)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ai_coding.cli.commands.github import make_host, resolve_repo
from ai_coding.cli.commands.run import _finish, _resolve
from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.integrations.github import GitHubHost
from ai_coding.integrations.github.auth import TokenAuth, auth_from_env
from ai_coding.integrations.github.client import DEFAULT_API_URL
from ai_coding.orchestrator import RunOptions, TaskPlanner
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


def _github_runtime(
    repo: str | None, root: Path
) -> tuple[GitHubHost, Callable[[], str | None], str]:
    host = make_host(resolve_repo(repo, root))
    auth = auth_from_env()

    def token() -> str | None:
        if isinstance(auth, TokenAuth):
            return auth.token
        return auth.authorization(host.client._exchange).removeprefix("Bearer ")

    api_host = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).split("://", 1)[-1].split("/")[0]
    return host, token, api_host


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
    host, token, api_host = _github_runtime(repo, root)
    request = PullRequestRequest(
        branch=branch, base=target, title=title, body=body, draft=draft, remote=remote
    )
    registry, steps = build_pull_request(request, host, token=token, api_host=api_host)
    plan = TaskPlanner().build(steps)
    orch = open_orchestrator(root, config, registry)
    options = RunOptions(require_approval=require_approval)
    if dry_run:
        for p in orch.preview(plan, options):
            verdict = p.approval.verdict.value if p.approval else "allow"
            info(escape(f"{p.key}: {p.handler or p.route_error} [{verdict}]"))
        info("dry run: nothing was pushed or opened")
        return
    run_id = guard(lambda: orch.create_run(plan, goal=f"pr: {branch} → {target}", options=options))
    _finish(guard(lambda: asyncio.run(orch.execute(run_id))), as_json=False)


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
    host, token, api_host = _github_runtime(repo, root)
    orch = open_orchestrator(
        root, config, pull_request_registry(host, token=token, api_host=api_host)
    )
    rid = _resolve(orch, run_id)
    _finish(guard(lambda: asyncio.run(orch.resume(rid))), as_json=False)
