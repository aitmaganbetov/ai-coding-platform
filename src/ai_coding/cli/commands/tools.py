"""``ai-coding tools`` — list analysis tools and pre-pull their sandbox images."""

from __future__ import annotations

import shutil
from typing import Annotated

import typer
from rich.table import Table

from ai_coding.cli.console import error, info, stdout
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.executors import DockerSandboxExecutor, docker_available
from ai_coding.executors.base import ExecutorError, ExecutorUnavailableError
from ai_coding.tools import default_tool_registry
from ai_coding.tools.testrun import RUNTIME_IMAGES

app = typer.Typer(help="Analysis tools and their sandbox images.", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """Analysis tools and their sandbox images."""


@app.command("list")
def list_tools() -> None:
    """Show tools, categories, pinned images and local availability."""
    registry = default_tool_registry()
    docker_ok = docker_available()
    executor = DockerSandboxExecutor()
    table = Table("tool", "category", "stacks", "host binary", "sandbox image")
    for name in registry.names():
        spec = registry.get(name).spec
        image_state = (
            ("present" if executor.image_present(spec.image) else "missing") if docker_ok
            else "docker unavailable"
        )  # fmt: skip
        table.add_row(
            name,
            spec.category.value,
            ", ".join(sorted(s.value for s in spec.stacks)) or "all",
            shutil.which(spec.binary) or "not found",
            f"{spec.image.split('@')[0]} ({image_state})",
        )
    for name, image in RUNTIME_IMAGES.items():
        state = (
            ("present" if executor.image_present(image) else "missing")
            if docker_ok
            else ("docker unavailable")
        )
        table.add_row(name, "tests runtime", "python" if "python" in name else "node",
                      "-", f"{image.split('@')[0]} ({state})")  # fmt: skip
    stdout.print(table)


@app.command("pull")
def pull(
    tool: Annotated[
        list[str] | None, typer.Option("--tool", "-t", help="Only these tools.")
    ] = None,
) -> None:
    """Download the digest-pinned sandbox images: analysis tools and test runtimes
    (``python-runtime``, ``node-runtime``). Needs network; audits and tests then run offline."""
    if not docker_available():
        error("Docker daemon is not reachable")
        raise typer.Exit(ExitCode.NOT_FOUND)
    registry = default_tool_registry()
    executor = DockerSandboxExecutor()
    failed = 0
    for name in tool or [*registry.names(), *RUNTIME_IMAGES]:
        if name in RUNTIME_IMAGES:
            image = RUNTIME_IMAGES[name]
        else:
            try:
                image = registry.get(name).spec.image
            except KeyError as exc:
                error(str(exc).strip("'\""))
                raise typer.Exit(ExitCode.USAGE) from exc
        if executor.image_present(image):
            info(f"{name}: already present")
            continue
        try:
            executor.pull(image)
            info(f"{name}: pulled {image}")
        except (ExecutorError, ExecutorUnavailableError) as exc:
            failed += 1
            error(f"{name}: {exc}")
    raise typer.Exit(ExitCode.INVALID if failed else ExitCode.OK)
