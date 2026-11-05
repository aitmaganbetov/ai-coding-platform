"""``ai-coding baseline`` — accept existing findings with owner and expiry (ТЗ §10)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.quality.baseline import BASELINE_FILENAME, baseline_from_findings
from ai_coding.tools.base import Finding

app = typer.Typer(help="Manage the findings baseline.", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """Manage the findings baseline."""


@app.command("create")
def create(
    report: Annotated[
        Path, typer.Argument(help="audit-report.json produced by `ai-coding audit`.")
    ],
    owner: Annotated[
        str, typer.Option("--owner", help="Person/team accountable for the exceptions.")
    ],
    reason: Annotated[str, typer.Option("--reason", help="Why these findings are accepted.")],
    expires: Annotated[str, typer.Option("--expires", help="Expiry date, YYYY-MM-DD.")],
    output: Annotated[Path, typer.Option("--output", "-o")] = Path(BASELINE_FILENAME),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a baseline accepting every *new* finding of an audit report."""
    try:
        expires_at = date.fromisoformat(expires)
    except ValueError as exc:
        error("--expires must be a date in YYYY-MM-DD format")
        raise typer.Exit(ExitCode.USAGE) from exc
    if expires_at <= date.today():
        error("--expires must be in the future")
        raise typer.Exit(ExitCode.USAGE)
    if not report.is_file():
        error(f"report not found: {report}")
        raise typer.Exit(ExitCode.NOT_FOUND)
    if output.exists() and not force:
        error(f"{output} exists (use --force to overwrite)")
        raise typer.Exit(ExitCode.INVALID)
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        findings = [Finding.from_dict(f) for f in data["findings"] if f.get("status") == "new"]
        baseline = baseline_from_findings(
            findings, owner=owner, reason=reason, expires_at=expires_at
        )
    except (KeyError, ValueError, TypeError) as exc:
        error(f"cannot build a baseline from {report}: {exc}")
        raise typer.Exit(ExitCode.INVALID) from exc
    output.write_text(baseline.to_json(), encoding="utf-8")
    info(f"wrote {len(baseline.entries)} entr(y/ies) to {output}; review and commit it")
