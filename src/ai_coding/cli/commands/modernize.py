"""``ai-coding modernize`` — MODERNIZE: baseline, impact analysis, plan, small PRs,
rollback (FR-05)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from ai_coding.cli.approvals import settle
from ai_coding.cli.commands.run import finish_run
from ai_coding.cli.common import close_all, load_project, open_orchestrator, resolve_run
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.cli.workflow import github_runtime, report_change, show_preview, start_or_resume
from ai_coding.domain.enums import Mode, RunState
from ai_coding.orchestrator import HandlerRegistry, RunOptions
from ai_coding.workflows.implement import ImplementSettings
from ai_coding.workflows.modernize import (
    ModernizeRequest,
    RollbackRequest,
    build_modernize,
    build_rollback,
)


def _show_plan(orch: Any, run_id: Any) -> None:
    planned = orch.task_output(run_id, "modernize.plan")
    if not planned:
        return
    plan = planned["plan"]
    info(f"\n[bold]Plan {escape(planned['version'])}[/]: {escape(plan['goal'])}")
    info(f"impact: {len(plan['impact']['files'])} file(s); "
         f"risks: {len(plan['impact']['risks'])}")  # fmt: skip
    for s in plan["steps"]:
        info(escape(f"  {s['id']} [{s['risk']}] {s['title']}"))


def _build(
    probe: Any,
    *,
    goal: str | None,
    step: str | None,
    continue_run: str | None,
    rollback: str | None,
    base: str,
    branch: str,
    allow_red_baseline: bool,
    settings: ImplementSettings,
    runtime: Any,
) -> tuple[Any, Any, str]:
    """Registry, steps and run goal for the chosen MODERNIZE action."""
    if rollback:
        rid = resolve_run(probe, rollback)
        source = (
            probe.task_output(rid, "modernize.step")
            or probe.task_output(rid, "develop.implement")
            or {}
        )
        registry, steps = build_rollback(
            RollbackRequest(str(rid), base=base, source_branch=source.get("branch"),
                            branch=branch), pull_request=runtime)  # fmt: skip
        return registry, steps, f"rollback: {rid}"
    if continue_run:
        rid = resolve_run(probe, continue_run)
        prev = probe.task_output(rid, "modernize.step")
        if not prev:
            error(f"run {continue_run} has no completed modernization step")
            raise typer.Exit(ExitCode.NOT_FOUND)
        next_id = step or prev.get("next_step")
        if not next_id:
            error("the plan has no further steps")
            raise typer.Exit(ExitCode.USAGE)
        request = ModernizeRequest(
            goal=prev["migration_plan"]["goal"], base=base, plan=prev["migration_plan"],
            plan_version=prev["plan_version"], step=next_id, branch=branch,
            allow_red_baseline=allow_red_baseline, settings=settings,
        )  # fmt: skip
        registry, steps = build_modernize(request, pull_request=runtime)
        return registry, steps, f"modernize {next_id}: {request.goal}"
    request = ModernizeRequest(
        goal=(goal or "").strip(), base=base, step=step, branch=branch,
        allow_red_baseline=allow_red_baseline, settings=settings,
    )  # fmt: skip
    registry, steps = build_modernize(request, pull_request=runtime)
    return registry, steps, f"modernize: {request.goal}"


def _report(orch: Any, run_id: Any, root: Path, *, rollback: bool) -> None:
    if rollback:
        out = orch.task_output(run_id, "rollback.revert") or {}
        if out.get("status") == "reverted":
            info(f"\n[green]reverted[/] on {escape(str(out['branch']))}")
        else:
            info(f"\n{escape(str(out.get('advice')))}")
        return
    report_change(orch, run_id, "modernize.step")
    nxt = (orch.task_output(run_id, "modernize.step") or {}).get("next_step")
    if nxt:
        info(f"next: ai-coding modernize --continue {run_id} -C {root}  (step {nxt})")


def modernize(
    ctx: typer.Context,
    goal: Annotated[str | None, typer.Argument(help="What to modernize.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path(),
    step: Annotated[str | None, typer.Option("--step", help="Plan step id, e.g. M-02.")] = None,
    continue_run: Annotated[
        str | None, typer.Option("--continue", help="Next step of an earlier run.")
    ] = None,
    rollback: Annotated[
        str | None, typer.Option("--rollback", help="Roll back the step of a run.")
    ] = None,
    base: Annotated[str | None, typer.Option("--base")] = None,
    branch: Annotated[str | None, typer.Option("--branch")] = None,
    max_iterations: Annotated[int, typer.Option("--max-iterations", min=1, max=10)] = 3,
    allow_red_baseline: Annotated[bool, typer.Option("--allow-red-baseline")] = False,
    pr: Annotated[bool, typer.Option("--pr", help="Push and open a PR.")] = False,
    repo: Annotated[str | None, typer.Option("--repo", "-R")] = None,
    resume: Annotated[str | None, typer.Option("--resume", help="Continue a waiting run.")] = None,
    interactive: Annotated[bool | None, typer.Option("--interactive/--no-interactive")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    require_approval: Annotated[bool, typer.Option("--require-approval")] = False,
) -> None:
    """Modernize step by step: one approved plan, one small verified PR per step."""
    ctx.call_on_close(close_all)
    if sum(bool(x) for x in (goal, continue_run, rollback)) != 1 and not resume:
        error("give exactly one of GOAL, --continue RUN or --rollback RUN")
        raise typer.Exit(ExitCode.USAGE)
    root, config = load_project(project)
    config = config.model_copy(update={"mode": Mode.MODERNIZE})
    interactive = sys.stdin.isatty() if interactive is None else interactive
    target = base or config.project.default_branch
    runtime = github_runtime(repo, root) if pr and not dry_run else None
    probe = open_orchestrator(root, config, build_rollback(RollbackRequest("x"))[0])
    if resume:  # any MODERNIZE or rollback run: inputs are persisted in its tasks
        registry = HandlerRegistry({
            h.name: h for r in (build_modernize(ModernizeRequest(goal="-"),
                                                pull_request=runtime)[0],
                                build_rollback(RollbackRequest("-"), pull_request=runtime)[0])
            for h in r.all()
        }.values())  # fmt: skip
        steps: Any = []
        goal_text = ""
    else:
        registry, steps, goal_text = _build(
            probe, goal=goal, step=step, continue_run=continue_run, rollback=rollback,
            base=target, branch=branch or "", allow_red_baseline=allow_red_baseline,
            settings=ImplementSettings(max_iterations=max_iterations), runtime=runtime,
        )  # fmt: skip
    orch = open_orchestrator(root, config, registry)
    options = RunOptions(require_approval=require_approval)
    if dry_run:
        show_preview(orch, steps, options)
        info("dry run: nothing changed")
        return
    run_id, status = start_or_resume(orch, steps, resume=resume, goal=goal_text, options=options)
    hint = f"ai-coding modernize --resume {run_id} -C {root} …(same options)"
    status = settle(orch, run_id, status, interactive=interactive, project=root,
                    resume_hint=hint, show=lambda _: _show_plan(orch, run_id))  # fmt: skip
    if status.state is RunState.COMPLETED:
        _report(orch, run_id, root, rollback=bool(rollback))
    finish_run(status, as_json=False)
