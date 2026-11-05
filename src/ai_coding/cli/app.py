"""``ai-coding`` entry point."""

from __future__ import annotations

from typing import Annotated

import typer

from ai_coding import __version__
from ai_coding.cli.commands import config as config_cmd
from ai_coding.cli.commands import init as init_cmd
from ai_coding.cli.commands import run as run_cmd

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
