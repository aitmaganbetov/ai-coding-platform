"""``ai-coding project add`` — import a local folder or a Git URL (FR-01)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import typer

from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config.loader import find_config
from ai_coding.detection import detect_stacks
from ai_coding.git import GitError, LocalGitRepository, git_available, validate_remote_url

app = typer.Typer(help="Import and inspect projects.", no_args_is_help=True)
_URL_LIKE = re.compile(r"^(https?://|ssh://|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:)")


@app.callback()
def _group() -> None:
    """Import and inspect projects."""


@app.command("add")
def add(
    source: Annotated[str, typer.Argument(help="Local directory or repository URL.")],
    dest: Annotated[
        Path | None, typer.Option("--dest", "-d", help="Clone destination (URL sources).")
    ] = None,
    branch: Annotated[str | None, typer.Option("--branch", help="Branch to clone.")] = None,
    depth: Annotated[int | None, typer.Option("--depth", min=1, help="Shallow clone depth.")] = 1,
    full_history: Annotated[
        bool, typer.Option("--full-history", help="Clone the whole history (no --depth).")
    ] = False,
) -> None:
    """Import a project: verify access and git state, detect stacks (FR-01, FR-02)."""
    if _URL_LIKE.match(source):
        if not git_available():
            error("git is required to clone repositories")
            raise typer.Exit(ExitCode.NOT_FOUND)
        try:
            validate_remote_url(source)
        except GitError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.USAGE) from exc
        name = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
        target = (dest or Path(name or "project")).resolve()
        info(f"cloning {source} → {target} (no submodules, hooks disabled)")
        try:
            repo = LocalGitRepository.clone(
                source, target, branch=branch, depth=None if full_history else depth
            )
        except GitError as exc:
            error(f"clone failed: {exc}")
            raise typer.Exit(ExitCode.INVALID) from exc
        root = repo.root
    else:
        root = Path(source).resolve()
        if not root.is_dir():
            error(f"directory not found: {root}")
            raise typer.Exit(ExitCode.NOT_FOUND)
    _describe(root)


def _describe(root: Path) -> None:
    info(f"project: {root}")
    if git_available() and LocalGitRepository.is_repository(root):
        try:
            repo = LocalGitRepository.open(root)
            status = repo.status()
            info(
                f"git: branch {status.branch or 'detached'}, head {(status.head or '-')[:12]}, "
                f"{'clean' if status.is_clean else f'{len(status.files)} changed file(s)'}"
            )
        except GitError as exc:
            error(f"git state unavailable: {exc}")
    else:
        info("git: not a repository (worktree isolation unavailable)")
    stacks = detect_stacks(root)
    info("stacks: " + (", ".join(f"{e.stack.value} ({e.path})" for e in stacks) or "none detected"))
    if find_config(root) is None:
        info(f"next: ai-coding init {root}  →  ai-coding audit -C {root}")
    else:
        info(f"next: ai-coding audit -C {root}")
