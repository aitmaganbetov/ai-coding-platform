"""``ai-coding`` entry point."""

from __future__ import annotations

from typing import Annotated

import typer

from ai_coding import __version__
from ai_coding.cli.commands import audit as audit_cmd
from ai_coding.cli.commands import baseline as baseline_cmd
from ai_coding.cli.commands import config as config_cmd
from ai_coding.cli.commands import github as github_cmd
from ai_coding.cli.commands import init as init_cmd
from ai_coding.cli.commands import pr as pr_cmd
from ai_coding.cli.commands import project as project_cmd
from ai_coding.cli.commands import report as report_cmd
from ai_coding.cli.commands import run as run_cmd
from ai_coding.cli.commands import tools as tools_cmd

app = typer.Typer(
    name="ai-coding",
    help=(
        "AI Coding Platform v2.1 — Local, Self-Hosted & GitHub Hybrid AI SDLC.\n\n"
        "Modes: CREATE, DEVELOP, MODERNIZE, AUDIT, MAINTAIN."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.command("init")(init_cmd.init)
app.add_typer(config_cmd.app, name="config")
app.add_typer(run_cmd.app, name="run")
app.command("audit")(audit_cmd.audit)
app.add_typer(baseline_cmd.app, name="baseline")
app.add_typer(project_cmd.app, name="project")
app.add_typer(tools_cmd.app, name="tools")
app.add_typer(report_cmd.app, name="report")
app.add_typer(github_cmd.app, name="github")
app.add_typer(pr_cmd.app, name="pr")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ai-coding {__version__}")
        raise typer.Exit


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", "-V", callback=_version, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """AI Coding Platform command-line interface."""


def main() -> None:
    """Console-script entry point."""
    app()
