"""``ai-coding audit`` — read-only, deterministic project audit (FR-06, S3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info, stdout
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import ExecutionBackend, RunState
from ai_coding.executors import docker_available
from ai_coding.orchestrator import RunOptions, TaskPlanner
from ai_coding.tools.semgrep import REGISTRY_CONFIG
from ai_coding.workflows import AuditOptions, build_audit


def audit(  # noqa: PLR0912 - option validation and output branches
    ctx: typer.Context,
    project: Annotated[
        Path, typer.Option("--project", "-C", help="Project directory.", show_default=False)
    ] = Path(),
    backend: Annotated[
        ExecutionBackend | None,
        typer.Option("--backend", "-b", help="Override execution.backend (local | docker)."),
    ] = None,
    tool: Annotated[
        list[str] | None, typer.Option("--tool", "-t", help="Run only these tools (repeatable).")
    ] = None,
    semgrep_config: Annotated[
        Path | None,
        typer.Option(
            "--semgrep-config",
            help="Local Semgrep rules file (registry configs are refused: offline).",
        ),
    ] = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat SKIPPED blocking gates as FAIL.")
    ] = False,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write audit-report.{json,md} here.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan and policy decisions only.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the JSON report.")] = False,
) -> None:
    """Read-only audit: secrets, SAST and lint in a sandbox → Quality Gates → report.

    Exit codes: 0 PASS, 1 FAIL (blocking gate or failed run), 2 not found / unavailable.
    """
    ctx.call_on_close(close_all)
    root, config = load_project(project)
    chosen = backend or config.execution.backend
    if chosen is ExecutionBackend.GITHUB_ACTIONS:
        error("the github-actions backend arrives in S4; use --backend local or docker")
        raise typer.Exit(ExitCode.USAGE)
    if semgrep_config is not None:
        if REGISTRY_CONFIG.match(str(semgrep_config)) or not semgrep_config.is_file():
            error("--semgrep-config must be a local rules file (audits run offline)")
            raise typer.Exit(ExitCode.USAGE)
        semgrep_config = semgrep_config.resolve()
    options = AuditOptions(backend=chosen, tools=tuple(tool or ()), semgrep_config=semgrep_config,
                           strict=strict)  # fmt: skip
    try:
        registry, steps, adapters = build_audit(config, root, options)
    except KeyError as exc:
        error(str(exc).strip("'\""))
        raise typer.Exit(ExitCode.USAGE) from exc
    plan = TaskPlanner().build(steps)
    orch = open_orchestrator(root, config, registry)

    if dry_run:
        table = Table("step", "phase", "handler", "policy")
        for p in orch.preview(plan, RunOptions()):
            table.add_row(p.key, p.phase.value, p.handler or f"[red]{p.route_error}[/]", "allow")
        stdout.print(table)
        info(
            f"backend: {chosen.value}; tools: {', '.join(a.spec.name for a in adapters) or 'none'}"
        )
        info("dry run: nothing was executed or stored")
        return
    if chosen is ExecutionBackend.DOCKER and not docker_available():
        error("Docker daemon is not reachable; start Docker or use --backend local")
        raise typer.Exit(ExitCode.NOT_FOUND)

    run_id = guard(lambda: orch.create_run(plan, goal="audit"))
    status = guard(lambda: asyncio.run(orch.execute(run_id)))
    if status.state is not RunState.COMPLETED:
        error(f"audit run {status.id} ended in {status.state.value}: {status.error or ''}")
        for t in status.tasks:
            if t["status"] == "FAILED":
                error(f"  {t['key']}: {t['error']}")
        raise typer.Exit(ExitCode.INVALID)

    artifacts = {a["name"]: a for a in status.summary.artifacts}
    report_json = orch.artifacts.get(artifacts["audit-report.json"]["sha256"]).decode("utf-8")
    report_md = orch.artifacts.get(artifacts["audit-report.md"]["sha256"]).decode("utf-8")
    report = json.loads(report_json)
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "audit-report.json").write_text(report_json, encoding="utf-8")
        (output / "audit-report.md").write_text(report_md, encoding="utf-8")
    if as_json:
        typer.echo(report_json, nl=False)
    else:
        _print_summary(report, output)
    raise typer.Exit(ExitCode.OK if report["verdict"] == "PASS" else ExitCode.INVALID)


def _print_summary(report: dict, output: Path | None) -> None:  # type: ignore[type-arg]
    verdict = report["verdict"]
    colour = "green" if verdict == "PASS" else "red"
    info(
        f"audit [bold]{report['project']}[/] — [bold {colour}]{verdict}[/] (run {report['run_id']})"
    )
    gates = Table("gate", "result", "blocking", "reason")
    for g in report["gates"]:
        gates.add_row(g["gate"], g["result"], "yes" if g["blocking"] else "no", g["reason"])
    stdout.print(gates)
    tools = Table("tool", "version", "status", "findings")
    for t in report["tools"]:
        status = "ok" if t["succeeded"] else f"error: {t['error']}"
        tools.add_row(t["tool"], t.get("version") or "?", status, str(len(t["findings"])))
    stdout.print(tools)
    summary = ", ".join(f"{k}: {v}" for k, v in report["summary"].items())
    info(f"new findings — {summary}; baselined: {report['baseline']['suppressed']}")
    info(f"integrity: repository unchanged = {report['integrity']['unchanged']}")
    if output is not None:
        info(f"reports: {output / 'audit-report.json'}, {output / 'audit-report.md'}")
