"""``ai-coding tools`` — list analysis tools and pre-pull their sandbox images."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Annotated

import typer
from rich.table import Table

from ai_coding.cli.console import error, info, stdout
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.executors import DockerSandboxExecutor, docker_available
from ai_coding.executors.base import ExecutorError, ExecutorUnavailableError
from ai_coding.tools import default_tool_registry
from ai_coding.tools.cache import cache_root
from ai_coding.tools.prepare import is_prepared, needs_prepare, prepare
from ai_coding.tools.project import ProjectTool
from ai_coding.tools.testrun import RUNTIME_IMAGES

app = typer.Typer(help="Analysis tools and their sandbox images.", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """Analysis tools and their sandbox images."""


def _image_state(executor: DockerSandboxExecutor, image: str, docker_ok: bool) -> str:
    if not docker_ok:
        return "docker unavailable"
    return "present" if executor.image_present(image) else "missing"


def _rows(docker_ok: bool) -> list[dict[str, str]]:
    registry = default_tool_registry()
    executor = DockerSandboxExecutor()
    rows = []
    for name in registry.names():
        adapter = registry.get(name)
        spec = adapter.spec
        host = "found" if shutil.which(spec.binary) else "not found"
        rows.append({
            "tool": name, "category": spec.category.value,
            "stacks": ", ".join(sorted(s.value for s in spec.stacks)) or "all",
            "runs": ("project toolchain (sandbox)" if isinstance(adapter, ProjectTool)
                     else f"analyser (host: {host})"),
            "image": spec.image, "image_state": _image_state(executor, spec.image, docker_ok),
            "offline_data": (("ready" if is_prepared(adapter) else "missing")
                             if needs_prepare(adapter) else "-"),
        })  # fmt: skip
    for name, image in RUNTIME_IMAGES.items():
        rows.append({
            "tool": name, "category": "tests runtime",
            "stacks": "python" if "python" in name else "node",
            "runs": "project tests (sandbox)", "image": image,
            "image_state": _image_state(executor, image, docker_ok), "offline_data": "-",
        })  # fmt: skip
    return rows


@app.command("list")
def list_tools(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show tools, categories, pinned images, offline data and local availability."""
    rows = _rows(docker_available())
    if as_json:
        # Plain text: machine-readable even when colour is forced (FORCE_COLOR in CI).
        typer.echo(json.dumps({"cache": str(cache_root()), "tools": rows}, indent=2))
        return
    table = Table("tool", "category", "stacks", "runs", "sandbox image", "offline data")
    for r in rows:
        table.add_row(r["tool"], r["category"], r["stacks"], r["runs"],
                      f"{r['image'].split('@')[0]} ({r['image_state']})",
                      r["offline_data"])  # fmt: skip
    stdout.print(table)
    info(f"platform cache: {cache_root()}")


@app.command("pull")
def pull(
    tool: Annotated[
        list[str] | None, typer.Option("--tool", "-t", help="Only these tools.")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-download offline data (new DB snapshot).")
    ] = False,
) -> None:
    """Download the digest-pinned sandbox images and the offline data (vulnerability
    databases, toolchains). The only networked step: audits and tests then run offline."""
    if not docker_available():
        error("Docker daemon is not reachable")
        raise typer.Exit(ExitCode.NOT_FOUND)
    registry = default_tool_registry()
    executor = DockerSandboxExecutor()
    failed = 0
    for name in tool or [*registry.names(), *RUNTIME_IMAGES]:
        adapter = None
        if name in RUNTIME_IMAGES:
            image = RUNTIME_IMAGES[name]
        else:
            try:
                adapter = registry.get(name)
            except KeyError as exc:
                error(str(exc).strip("'\""))
                raise typer.Exit(ExitCode.USAGE) from exc
            image = adapter.spec.image
        try:
            if executor.image_present(image):
                info(f"{name}: image already present")
            else:
                executor.pull(image)
                info(f"{name}: pulled {image}")
            if adapter is not None and needs_prepare(adapter):
                if is_prepared(adapter) and not refresh:
                    info(f"{name}: offline data already prepared")
                else:
                    asyncio.run(prepare(adapter))
                    info(f"{name}: offline data prepared in {cache_root()}")
        except (ExecutorError, ExecutorUnavailableError, RuntimeError) as exc:
            failed += 1
            error(f"{name}: {exc}")
    raise typer.Exit(ExitCode.INVALID if failed else ExitCode.OK)
