"""``ai-coding report export`` — audit report as JSON, Markdown, SARIF or JUnit (FR-10)."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from ai_coding.cli.common import close_all, guard, load_project, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.orchestrator.handlers import builtin_registry
from ai_coding.reports import AuditReport, render_junit, render_markdown, render_sarif

app = typer.Typer(help="Export reports.", no_args_is_help=True)


class Format(StrEnum):
    """Export formats."""

    JSON = "json"
    MARKDOWN = "markdown"
    SARIF = "sarif"
    JUNIT = "junit"


@app.callback()
def _group(ctx: typer.Context) -> None:
    """Export reports."""
    ctx.call_on_close(close_all)


def _load_report(run_id: str | None, input_file: Path | None, project: Path) -> dict[str, Any]:
    if input_file is not None:
        if not input_file.is_file():
            error(f"report not found: {input_file}")
            raise typer.Exit(ExitCode.NOT_FOUND)
        data: dict[str, Any] = json.loads(input_file.read_text(encoding="utf-8"))
        return data
    if run_id is None:
        error("give a RUN id or --input audit-report.json")
        raise typer.Exit(ExitCode.USAGE)
    root, config = load_project(project)
    orch = open_orchestrator(root, config, builtin_registry())
    rid = guard(lambda: orch.resolve_run_id(run_id))
    artifacts = {a["name"]: a for a in guard(lambda: orch.status(rid)).summary.artifacts}
    if "audit-report.json" not in artifacts:
        error(f"run {run_id} has no audit report (only `ai-coding audit` runs produce one)")
        raise typer.Exit(ExitCode.NOT_FOUND)
    loaded: dict[str, Any] = json.loads(
        orch.artifacts.get(artifacts["audit-report.json"]["sha256"])
    )
    return loaded


@app.command("export")
def export(
    run_id: Annotated[str | None, typer.Argument(help="Audit run id (or use --input).")] = None,
    fmt: Annotated[Format, typer.Option("--format", "-f")] = Format.SARIF,
    input_file: Annotated[
        Path | None, typer.Option("--input", "-i", help="Convert an existing audit-report.json.")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path(),
) -> None:
    """Export an audit report (SARIF for code scanning, JUnit for CI test reporters)."""
    report = _load_report(run_id, input_file, project)
    try:
        if fmt is Format.SARIF:
            text = render_sarif(report)
        elif fmt is Format.JUNIT:
            text = render_junit(report)
        elif fmt is Format.MARKDOWN:
            fields = AuditReport.__dataclass_fields__
            text = render_markdown(AuditReport(**{k: v for k, v in report.items() if k in fields}))
        else:
            text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    except (KeyError, TypeError, ValueError) as exc:
        error(f"not an audit report: {exc}")
        raise typer.Exit(ExitCode.INVALID) from exc
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text, encoding="utf-8")
        info(f"written {output}")
