"""``ai-coding init`` — create ``.ai-coding.yml`` for a project."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config.generator import (
    ConfigGenerationError,
    render_initial_config,
    sanitize_project_name,
)
from ai_coding.config.loader import CONFIG_FILENAME
from ai_coding.detection import detect_stacks
from ai_coding.domain.enums import Mode, Stack
from ai_coding.git import GitError, LocalGitRepository, git_available


def init(
    path: Annotated[
        Path,
        typer.Argument(help="Project directory (default: current directory).", show_default=False),
    ] = Path(),
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Project name (default: directory name).")
    ] = None,
    mode: Annotated[
        Mode, typer.Option("--mode", "-m", help="Default lifecycle mode.")
    ] = Mode.AUDIT,
    stack: Annotated[
        list[Stack] | None,
        typer.Option("--stack", "-s", help="Override detected stacks (repeatable)."),
    ] = None,
    detect: Annotated[
        bool, typer.Option("--detect/--no-detect", help="Detect stacks from manifest files.")
    ] = True,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing configuration file.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation (with --force).")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the configuration instead of writing it.")
    ] = False,
) -> None:
    """Create a validated .ai-coding.yml with secure defaults."""
    root = path.resolve()
    if not root.is_dir():
        error(f"directory not found: {root}")
        raise typer.Exit(ExitCode.NOT_FOUND)

    target = root / CONFIG_FILENAME
    if target.exists() and not force and not dry_run:
        error(f"{target} already exists (use --force to overwrite)")
        raise typer.Exit(ExitCode.INVALID)

    git_line, default_branch = _describe_git(root)
    stacks, stack_line = _resolve_stacks(root, stack, detect=detect)

    project_name = sanitize_project_name(name if name is not None else root.name)
    try:
        text, _ = render_initial_config(
            name=project_name, mode=mode, stacks=stacks, default_branch=default_branch
        )
    except ConfigGenerationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.INVALID) from exc

    if dry_run:
        typer.echo(text, nl=False)
        return

    if target.exists():
        if not yes and not typer.confirm(f"Overwrite {target}?", default=False):
            error("aborted; existing configuration left unchanged")
            raise typer.Exit(ExitCode.ABORTED)
        backup = target.with_name(target.name + ".bak")
        backup.write_bytes(target.read_bytes())
        info(f"backup: {backup}")

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(target)

    info(git_line)
    info(stack_line)
    info(f"[green]created[/] {target}")
    info("next: review the file, then run `ai-coding config validate`")


def _guess_default_branch(branches: list[str], current: str | None) -> str:
    for candidate in ("main", "master"):
        if candidate in branches:
            return candidate
    return current or "main"


def _describe_git(root: Path) -> tuple[str, str]:
    """Return a human summary of the git state (FR-01) and the guessed default branch."""
    if not (git_available() and LocalGitRepository.is_repository(root)):
        return "git: not a repository (platform still works; worktree isolation needs git)", "main"
    try:
        repo = LocalGitRepository.open(root)
        status = repo.status()
        branches = repo.branches()
    except GitError as exc:
        return f"git: unavailable ({exc})", "main"
    branch = status.branch or "detached HEAD"
    state = "clean" if status.is_clean else f"{len(status.files)} changed file(s)"
    return f"git: {repo.root} on {branch} ({state})", _guess_default_branch(branches, status.branch)


def _resolve_stacks(
    root: Path, override: list[Stack] | None, *, detect: bool
) -> tuple[list[Stack], str]:
    if override:
        stacks = list(dict.fromkeys(override))
        return stacks, "stacks: " + ", ".join(s.value for s in stacks) + " (from --stack)"
    if not detect:
        return [], "stacks: detection disabled"
    evidence = detect_stacks(root)
    described = ", ".join(f"{e.stack.value} ({e.path})" for e in evidence) or "none detected"
    return [e.stack for e in evidence], f"stacks: {described}"
