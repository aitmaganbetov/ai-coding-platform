"""``ai-coding maintain`` — MAINTAIN: triage issues, findings and vulnerable
dependencies into an approved plan; one small verified PR per item (B-610, B-612)."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from ai_coding.cli.approvals import settle
from ai_coding.cli.commands.develop import report_change
from ai_coding.cli.commands.pr import _github_runtime
from ai_coding.cli.commands.run import _finish, _resolve
from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import Mode, RunState
from ai_coding.orchestrator import HandlerRegistry, RunOptions, TaskPlanner
from ai_coding.workflows.implement import ImplementSettings
from ai_coding.workflows.maintain import MaintainRequest, build_maintain, sources_from_report


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        error(f"{what} not found: {path}")
        raise typer.Exit(ExitCode.NOT_FOUND) from None
    except ValueError as exc:
        error(f"{what} is not valid JSON: {exc}")
        raise typer.Exit(ExitCode.INVALID) from None


def _issues(numbers: list[int], issues_file: Path | None, runtime: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if issues_file is not None:
        data = _read_json(issues_file, "issues file")
        if not isinstance(data, list) or not all(isinstance(i, dict) and "title" in i
                                                 for i in data):  # fmt: skip
            error("the issues file must be a JSON list of {id, title, body} objects")
            raise typer.Exit(ExitCode.INVALID)
        issues += [{"id": str(i.get("id") or f"I{n}"), "title": str(i["title"]),
                    "body": str(i.get("body") or "")} for n, i in enumerate(data, 1)]  # fmt: skip
    host: Any = runtime[0] if runtime is not None else None
    for number in numbers:
        found = guard(lambda: host.get_issue(number))  # noqa: B023 - called immediately
        issues.append({"id": f"#{found.number}", "title": found.title, "body": found.body})
    return issues


def _show_plan(orch: Any, run_id: Any) -> None:
    planned = orch.task_output(run_id, "maintain.plan")
    if not planned:
        return
    plan = planned["plan"]
    info(f"\n[bold]Maintenance plan {escape(planned['version'])}[/]: {escape(plan['summary'])}")
    for i in plan["items"]:
        upgrades = ", ".join(f"{u['source']}→{u['to_version']}" for u in i.get("upgrades", []))
        info(escape(f"  {i['id']} [{i['priority']}/{i['kind']}] {i['title']}"
                    + (f" (upgrade {upgrades})" if upgrades else "")
                    + f" — test {i['regression_test']['file']}"))  # fmt: skip
    for d in plan.get("deferred", []):
        info(escape(f"  deferred {d['source']}: {d['reason']}"))


def _continued(
    probe: Any,
    run: str,
    item: str | None,
    base: str,
    *,
    allow_red_baseline: bool,
    settings: ImplementSettings,
) -> tuple[MaintainRequest, list[str], str]:
    """The next item of an earlier run's approved plan."""
    rid = _resolve(probe, run)
    prev = probe.task_output(rid, "maintain.item")
    if not prev:
        error(f"run {run} has no completed maintenance item")
        raise typer.Exit(ExitCode.NOT_FOUND)
    next_id = item or prev.get("next_item")
    if not next_id:
        error("every item of the plan is done")
        raise typer.Exit(ExitCode.USAGE)
    request = MaintainRequest(
        base=base, item=next_id, plan=prev["maintenance_plan"],
        plan_version=prev["plan_version"], sources=prev["sources"],
        allow_red_baseline=allow_red_baseline, settings=settings,
    )  # fmt: skip
    return request, list(prev["done"]), f"maintain {next_id} ({prev['plan_version']})"


def _new_request(
    report: Path | None,
    issue_numbers: list[int],
    issues_file: Path | None,
    telemetry: Path | None,
    runtime: Any,
    *,
    dry_run: bool,
) -> MaintainRequest:
    """Sources of a new plan (exits with USAGE when there is nothing to maintain)."""
    findings: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    if report is not None:
        data = _read_json(report, "audit report")
        if not isinstance(data, dict) or "findings" not in data:
            error("--report must be an audit-report.json (ai-coding audit -o DIR)")
            raise typer.Exit(ExitCode.INVALID)
        findings, dependencies = sources_from_report(data)
    # A dry run reads local files but never calls GitHub.
    issues = _issues([] if dry_run else issue_numbers, issues_file, runtime)
    text = telemetry.read_text(encoding="utf-8")[:20_000] if telemetry else None
    if not (issues or findings or dependencies or text or (dry_run and issue_numbers)):
        error("nothing to maintain: give --report, --issue, --issues-file or --telemetry")
        raise typer.Exit(ExitCode.USAGE)
    return MaintainRequest(issues=tuple(issues), findings=tuple(findings),
                           dependencies=tuple(dependencies), telemetry=text)  # fmt: skip


def maintain(
    ctx: typer.Context,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path(),
    report: Annotated[
        Path | None, typer.Option("--report", help="audit-report.json (findings, vulnerable deps).")
    ] = None,
    issue: Annotated[
        list[int] | None, typer.Option("--issue", "-i", help="GitHub issue (repeatable).")
    ] = None,
    issues_file: Annotated[
        Path | None, typer.Option("--issues-file", help="JSON list of {id, title, body}.")
    ] = None,
    telemetry: Annotated[
        Path | None, typer.Option("--telemetry", help="Error/incident summary (text).")
    ] = None,
    continue_run: Annotated[
        str | None, typer.Option("--continue", help="Next item of an earlier run's plan.")
    ] = None,
    item: Annotated[str | None, typer.Option("--item", help="Plan item id, e.g. M2.")] = None,
    base: Annotated[str | None, typer.Option("--base")] = None,
    max_iterations: Annotated[int, typer.Option("--max-iterations", min=1, max=10)] = 3,
    allow_red_baseline: Annotated[bool, typer.Option("--allow-red-baseline")] = False,
    pr: Annotated[bool, typer.Option("--pr", help="Push and open a PR.")] = False,
    repo: Annotated[str | None, typer.Option("--repo", "-R")] = None,
    resume: Annotated[str | None, typer.Option("--resume", help="Continue a waiting run.")] = None,
    interactive: Annotated[bool | None, typer.Option("--interactive/--no-interactive")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Triage → approved plan → one item per run: upgrade, fix, regression test, PR."""
    ctx.call_on_close(close_all)
    root, config = load_project(project)
    config = config.model_copy(update={"mode": Mode.MAINTAIN})
    interactive = sys.stdin.isatty() if interactive is None else interactive
    target = base or config.project.default_branch
    runtime = _github_runtime(repo, root) if (pr or issue) and not dry_run else None
    settings = ImplementSettings(max_iterations=max_iterations)
    probe = open_orchestrator(root, config, HandlerRegistry([]))
    if resume:  # inputs are persisted in the run's tasks
        registry = build_maintain(MaintainRequest(), pull_request=runtime)[0]
        steps: Any = []
        goal = ""
    elif continue_run:
        request, done, goal = _continued(probe, continue_run, item, target,
                                         allow_red_baseline=allow_red_baseline,
                                         settings=settings)  # fmt: skip
        registry, steps = build_maintain(request, pull_request=runtime, done=done)
    else:
        request = _new_request(report, list(issue or []), issues_file, telemetry, runtime,
                               dry_run=dry_run)  # fmt: skip
        request = replace(
            request,
            base=target,
            item=item,
            allow_red_baseline=allow_red_baseline,
            settings=settings,
        )
        registry, steps = build_maintain(request, pull_request=runtime)
        goal = (f"maintain: {len(request.issues)} issue(s), {len(request.findings)} "
                f"finding(s), {len(request.dependencies)} vulnerable dependenc(y/ies)")  # fmt: skip
    orch = open_orchestrator(root, config, registry)
    if dry_run:
        for p in orch.preview(TaskPlanner().build(steps), RunOptions()):
            info(escape(f"{p.key}: {p.handler or p.route_error}"))
        info("dry run: nothing changed")
        return
    if resume:
        run_id = _resolve(orch, resume)
        status = guard(lambda: asyncio.run(orch.resume(run_id)))
    else:
        run_id = guard(lambda: orch.create_run(TaskPlanner().build(steps), goal=goal[:200]))
        status = guard(lambda: asyncio.run(orch.execute(run_id)))
    hint = f"ai-coding maintain --resume {run_id} -C {root}" + (" --pr" if pr else "")
    status = settle(orch, run_id, status, interactive=interactive, project=root,
                    resume_hint=hint, show=lambda _: _show_plan(orch, run_id))  # fmt: skip
    if status.state is RunState.COMPLETED:
        report_change(orch, run_id, "maintain.item")
        nxt = (orch.task_output(run_id, "maintain.item") or {}).get("next_item")
        if nxt:
            info(f"next: ai-coding maintain --continue {run_id} -C {root}  (item {nxt})")
    _finish(status, as_json=False)
