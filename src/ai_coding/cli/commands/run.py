"""``ai-coding run`` — start, inspect, resume, cancel and approve runs (ТЗ §14)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info, stdout
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import RunState, TaskStatus
from ai_coding.orchestrator import Orchestrator, RunOptions, RunStatus, TaskPlanner
from ai_coding.orchestrator.engine import short_id
from ai_coding.orchestrator.handlers import builtin_registry, discovery_plan

app = typer.Typer(help="Start and manage orchestrated runs.", no_args_is_help=True)


@app.callback()
def _group(ctx: typer.Context) -> None:
    """Start and manage orchestrated runs."""
    # Release databases when the command finishes, however it exits.
    ctx.call_on_close(close_all)


ProjectOpt = Annotated[
    Path, typer.Option("--project", "-C", help="Project directory.", show_default=False)
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]

_STATE_EXIT = {
    RunState.COMPLETED: ExitCode.OK,
    RunState.FAILED: ExitCode.INVALID,
    RunState.CANCELLED: ExitCode.INVALID,
    RunState.AWAITING_APPROVAL: ExitCode.PENDING,
    RunState.PAUSED: ExitCode.PENDING,
}


def _orchestrator(project: Path) -> Orchestrator:
    root, config = load_project(project)
    return open_orchestrator(root, config, builtin_registry())


_guard = guard


def _print_status(status: RunStatus, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(status.to_dict(), indent=2, ensure_ascii=False))
        return
    info(f"run [bold]{status.id}[/] — {status.state.value} ({status.mode})")
    if status.error:
        info(f"error: {status.error}")
    table = Table("task", "phase", "status", "attempts", "error", show_lines=False)
    for task in status.tasks:
        table.add_row(
            task["key"], task["phase"], task["status"], str(task["attempts"]), task["error"] or ""
        )
    stdout.print(table)
    pending = [a for a in status.approvals if a["status"] == "pending"]
    for approval in pending:
        info(
            f"approval needed [{short_id(approval['id'])}] "
            f"{approval['operation']}: {approval['reason']}"
        )
    if pending:
        info(f"approve with: ai-coding run approve {short_id(status.id)} --resume")
    for artifact in status.summary.artifacts:
        info(f"artifact {artifact['name']}: {artifact['uri']}")


def _finish(status: RunStatus, as_json: bool) -> None:
    _print_status(status, as_json)
    raise typer.Exit(_STATE_EXIT.get(status.state, ExitCode.OK))


@app.command("discover")
def discover(
    project: ProjectOpt = Path(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan and policy decisions only.")
    ] = False,
    require_approval: Annotated[
        bool, typer.Option("--require-approval", help="Require approval for every operation.")
    ] = False,
    idempotency_key: Annotated[
        str | None, typer.Option("--idempotency-key", help="Re-use an existing identical run.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Run DISCOVERY: config, git state, stacks, context index → report (offline)."""
    orch = _orchestrator(project)
    plan = TaskPlanner().build(
        discovery_plan(max_attempts=orch.config.execution.limits.max_retries + 1)
    )
    options = RunOptions(require_approval=require_approval)
    if dry_run:
        previews = orch.preview(plan, options)
        if as_json:
            typer.echo(
                json.dumps(
                    [
                        {
                            "key": p.key,
                            "phase": p.phase.value,
                            "capability": p.capability,
                            "handler": p.handler,
                            "route_error": p.route_error,
                            "approval": p.approval.verdict.value if p.approval else None,
                        }
                        for p in previews
                    ],
                    indent=2,
                )
            )
            return
        table = Table("step", "phase", "handler", "policy")
        for p in previews:
            table.add_row(
                p.key,
                p.phase.value,
                p.handler or f"[red]{p.route_error}[/]",
                p.approval.verdict.value if p.approval else "allow",
            )
        stdout.print(table)
        info("dry run: nothing was executed or stored")
        return
    run_id = _guard(
        lambda: orch.create_run(
            plan, goal="discovery", idempotency_key=idempotency_key, options=options
        )
    )
    status = _guard(lambda: asyncio.run(orch.execute(run_id)))
    _finish(status, as_json)


@app.command("list")
def list_runs(
    project: ProjectOpt = Path(),
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 20,
    as_json: JsonOpt = False,
) -> None:
    """List recent runs of the project."""
    runs = _orchestrator(project).list_runs(limit)
    if as_json:
        typer.echo(json.dumps(runs, indent=2))
        return
    table = Table("id", "state", "mode", "goal", "created")
    for r in runs:
        table.add_row(short_id(r["id"]), r["state"], r["mode"], r["goal"], r["created_at"][:19])
    stdout.print(table)


def _resolve(orch: Orchestrator, run_id: str) -> uuid.UUID:
    return _guard(lambda: orch.resolve_run_id(run_id))


_RESUME_HINTS = {"git.push": "ai-coding pr resume", "github.pr": "ai-coding pr resume"}


def _ensure_resumable(orch: Orchestrator, rid: uuid.UUID) -> None:
    """Refuse to resume a run whose remaining steps need handlers this command lacks.

    Resuming anyway would fail those steps with "no route" and end the run as FAILED,
    destroying a run that its own workflow command (e.g. ``pr resume``) could finish.
    """
    unfinished = [
        t["capability"]
        for t in _guard(lambda: orch.status(rid)).tasks
        if t["status"] not in {TaskStatus.SUCCEEDED.value, TaskStatus.SKIPPED.value}
    ]
    missing = sorted({c for c in unfinished if not orch.registry.for_capability(c)})
    if missing:
        hint = next((_RESUME_HINTS[c] for c in missing if c in _RESUME_HINTS), None)
        error(
            f"run {short_id(rid)} needs handlers for {', '.join(missing)}"
            + (f"; resume it with `{hint} {short_id(rid)}`" if hint else "")
        )
        raise typer.Exit(ExitCode.USAGE)


RunIdArg = Annotated[str, typer.Argument(help="Run id or unique prefix.")]


@app.command("status")
def status(run_id: RunIdArg, project: ProjectOpt = Path(), as_json: JsonOpt = False) -> None:
    """Show state, tasks, approvals and artifacts of a run."""
    orch = _orchestrator(project)
    rid = _resolve(orch, run_id)
    _print_status(_guard(lambda: orch.status(rid)), as_json)


@app.command("events")
def events(run_id: RunIdArg, project: ProjectOpt = Path(), as_json: JsonOpt = False) -> None:
    """Show the state transition journal of a run."""
    orch = _orchestrator(project)
    rid = _resolve(orch, run_id)
    rows = _guard(lambda: orch.events(rid))
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table("#", "from", "to", "actor", "reason", "at")
    for e in rows:
        table.add_row(
            str(e["seq"]), e["from"] or "", e["to"], e["actor"], e["reason"] or "", e["at"][:19]
        )
    stdout.print(table)


@app.command("audit")
def audit(
    run_id: RunIdArg,
    project: ProjectOpt = Path(),
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write JSON to a file.")
    ] = None,
) -> None:
    """Export the audit trail of a run as JSON (AC-10)."""
    orch = _orchestrator(project)
    rid = _resolve(orch, run_id)
    text = json.dumps(_guard(lambda: orch.audit_trail(rid)), indent=2, ensure_ascii=False)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        info(f"written {output}")


@app.command("resume")
def resume(
    run_id: RunIdArg,
    project: ProjectOpt = Path(),
    force: Annotated[
        bool, typer.Option("--force", help="Take over a run whose lease has not expired.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Continue a run from its last confirmed checkpoint (after crash, pause or approval)."""
    orch = _orchestrator(project)
    rid = _resolve(orch, run_id)
    if (
        force
        and not yes
        and not typer.confirm(
            "Force-taking the lease can run a step twice if the other worker is alive. Continue?",
            default=False,
        )
    ):
        raise typer.Exit(ExitCode.ABORTED)
    _ensure_resumable(orch, rid)
    _finish(_guard(lambda: asyncio.run(orch.resume(rid, force=force))), as_json)


@app.command("cancel")
def cancel(
    run_id: RunIdArg,
    project: ProjectOpt = Path(),
    reason: Annotated[str, typer.Option("--reason")] = "cancelled by user",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Cancel a run (cooperatively if a worker is executing it)."""
    orch = _orchestrator(project)
    rid = _resolve(orch, run_id)
    current = _guard(lambda: orch.status(rid))
    if dry_run:
        info(f"would cancel run {rid} in state {current.state.value}")
        return
    if not yes and not typer.confirm(f"Cancel run {short_id(rid)} ({current.state.value})?"):
        raise typer.Exit(ExitCode.ABORTED)
    _print_status(_guard(lambda: orch.cancel(rid, reason=reason)), as_json)


def _decide(
    run_id: str, project: Path, *, approve: bool, approval: str | None, note: str | None
) -> tuple[Orchestrator, uuid.UUID, list[Any]]:
    orch = _orchestrator(project)
    rid = _resolve(orch, run_id)
    approval_id: uuid.UUID | None = None
    if approval:
        ref = approval.lower()
        candidates = [
            a["id"]
            for a in orch.status(rid).approvals
            if a["id"].startswith(ref) or a["id"].endswith(ref)
        ]
        if len(candidates) != 1:
            error(f"approval {approval!r} not found or ambiguous")
            raise typer.Exit(ExitCode.NOT_FOUND)
        approval_id = uuid.UUID(candidates[0])
    decided = _guard(lambda: orch.decide(rid, approve=approve, approval_id=approval_id, note=note))
    return orch, rid, decided


@app.command("approve")
def approve(
    run_id: RunIdArg,
    project: ProjectOpt = Path(),
    approval: Annotated[str | None, typer.Option("--approval", help="Approval id prefix.")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    resume_run: Annotated[
        bool, typer.Option("--resume", help="Resume the run afterwards.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Approve pending approvals (all, or one with --approval). Owner/Maintainer only."""
    orch, rid, decided = _decide(run_id, project, approve=True, approval=approval, note=note)
    for a in decided:
        info(f"[green]approved[/] {a.operation} ({short_id(a.id)})")
    if resume_run:
        _ensure_resumable(orch, rid)
        _finish(_guard(lambda: asyncio.run(orch.resume(rid))), as_json)


@app.command("reject")
def reject(
    run_id: RunIdArg,
    project: ProjectOpt = Path(),
    approval: Annotated[str | None, typer.Option("--approval", help="Approval id prefix.")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Reject pending approvals; the run is cancelled when resumed."""
    _, _, decided = _decide(run_id, project, approve=False, approval=approval, note=note)
    for a in decided:
        info(f"[red]rejected[/] {a.operation} ({short_id(a.id)})")
