"""``ai-coding config`` — validate and inspect ``.ai-coding.yml``."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ai_coding.cli.console import error, info, stdout
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config.loader import CONFIG_FILENAME, find_config
from ai_coding.config.schema import render_schema
from ai_coding.config.validator import validate_file

app = typer.Typer(help="Validate and inspect project configuration.", no_args_is_help=True)


class OutputFormat(StrEnum):
    """Output format for machine consumption."""

    TEXT = "text"
    JSON = "json"


@app.command("validate")
def validate(
    path: Annotated[
        Path,
        typer.Argument(
            help=f"Configuration file or project directory (default: ./{CONFIG_FILENAME}).",
            show_default=False,
        ),
    ] = Path(),
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format.")
    ] = OutputFormat.TEXT,
) -> None:
    """Validate .ai-coding.yml against the JSON Schema and security policy.

    Exit codes: 0 valid, 1 invalid, 2 file not found.
    """
    config_path = find_config(path) if path.exists() else None
    if config_path is None:
        message = f"configuration not found: {path / CONFIG_FILENAME if path.is_dir() else path}"
        if output_format is OutputFormat.JSON:
            typer.echo(json.dumps({"source": str(path), "valid": False, "error": message}))
        else:
            error(message)
        raise typer.Exit(ExitCode.NOT_FOUND)

    report = validate_file(config_path)
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    elif report.ok:
        info(f"[green]valid[/] {config_path}")
    else:
        error(f"{config_path} is invalid ({len(report.issues)} issue(s))")
        for issue in report.issues:
            where = issue.path or "<root>"
            line = f" (line {issue.line})" if issue.line else ""
            stdout.print(f"  [{issue.kind.value}] {where}{line}: {issue.message}", markup=False)
    raise typer.Exit(ExitCode.OK if report.ok else ExitCode.INVALID)


@app.command("schema")
def schema(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to a file instead of stdout.")
    ] = None,
) -> None:
    """Print the JSON Schema (Draft 2020-12) for .ai-coding.yml."""
    text = render_schema()
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text, encoding="utf-8", newline="\n")
    info(f"written {output}")
