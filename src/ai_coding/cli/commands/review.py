"""``ai-coding review`` — advisory AI review of a diff; used by ``ai-review.yml`` (B-507)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from ai_coding.cli.commands.github import make_host, resolve_repo
from ai_coding.cli.commands.run import _finish
from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import RunState
from ai_coding.integrations.github.host import Annotation
from ai_coding.orchestrator import TaskPlanner
from ai_coding.workflows.review import ReviewRequest, build_review

_LEVEL = {"critical": "failure", "high": "failure", "medium": "warning"}


def review(
    ctx: typer.Context,
    base: Annotated[str, typer.Option("--base", help="Base ref, e.g. origin/main.")],
    head: Annotated[str, typer.Option("--head")] = "HEAD",
    project: Annotated[Path, typer.Option("--project", "-C")] = Path(),
    title: Annotated[str, typer.Option("--title", help="What the change is about.")] = "",
    pr: Annotated[int | None, typer.Option("--pr", help="PR number for --comment.")] = None,
    comment: Annotated[bool, typer.Option("--comment", help="Upsert a PR comment.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Publish a check run on HEAD.")] = False,
    repo: Annotated[str | None, typer.Option("--repo", "-R")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    skip_if_disabled: Annotated[
        bool, typer.Option("--skip-if-disabled", help="Exit 0 when AI is disabled.")
    ] = False,
) -> None:
    """Review the diff BASE…HEAD with the Reviewer Agent (advisory, ТЗ §10)."""
    ctx.call_on_close(close_all)
    root, config = load_project(project)
    if not config.ai.enabled and skip_if_disabled:
        info("AI review skipped: AI is disabled in the project configuration")
        return
    if comment and pr is None:
        error("--comment needs --pr")
        raise typer.Exit(ExitCode.USAGE)
    registry, steps = build_review(ReviewRequest(base=base, head=head, title=title))
    orch = open_orchestrator(root, config, registry)
    run_id = guard(
        lambda: orch.create_run(TaskPlanner().build(steps), goal=f"review {base}…{head}")
    )
    status = guard(lambda: asyncio.run(orch.execute(run_id)))
    if status.state is not RunState.COMPLETED:
        _finish(status, as_json=False)
    out = orch.task_output(run_id, "review.run") or {}
    text = json.dumps(out["review"], indent=2, ensure_ascii=False) if as_json else out["markdown"]
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        typer.echo(text)
    if comment or check:
        host = make_host(resolve_repo(repo, root))
        if comment and pr is not None:
            guard(lambda: host.upsert_comment(pr, "ai-review", out["markdown"]))
        if check:
            findings = out["review"]["findings"]
            notes = [
                Annotation(path=f["file"], start_line=f["line"],
                           level=_LEVEL.get(f["severity"], "notice"),
                           title=f"{f['severity']}: {f['category']}", message=f["message"])
                for f in findings
            ]  # fmt: skip
            guard(lambda: host.create_check_run(
                name="ai-coding / ai-review", head_sha=out["head"], conclusion=out["conclusion"],
                title=f"AI review: {out['review']['verdict']} ({len(findings)} finding(s))",
                summary=out["markdown"], annotations=notes,
            ))  # fmt: skip
    raise typer.Exit(ExitCode.INVALID if out["conclusion"] == "failure" else ExitCode.OK)
