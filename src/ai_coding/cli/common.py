"""Helpers shared by CLI commands that open a project's orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from ai_coding.cli.console import error
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config.loader import find_project_config
from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import validate_file
from ai_coding.observability import configure_logging
from ai_coding.orchestrator import HandlerRegistry, Orchestrator
from ai_coding.orchestrator.errors import (
    ApprovalError,
    IdempotencyConflictError,
    InvalidTransitionError,
    OrchestratorError,
    RunLeaseError,
    RunNotFoundError,
)
from ai_coding.persistence import Database, upgrade_to_head

_OPENED: list[Orchestrator] = []


def close_all() -> None:
    """Release every orchestrator opened by the current command."""
    while _OPENED:
        _OPENED.pop().close()


def load_project(project: Path) -> tuple[Path, AICodingConfig]:
    """Resolve the project directory and load its valid configuration (or exit)."""
    root = project.resolve()
    config_path = find_project_config(root)
    if config_path is None:
        error(f"configuration not found in {root} (run `ai-coding init`)")
        raise typer.Exit(ExitCode.NOT_FOUND)
    report = validate_file(config_path)
    if not report.ok or report.config is None:
        error(f"{config_path} is invalid; run `ai-coding config validate`")
        raise typer.Exit(ExitCode.INVALID)
    return root, report.config


def open_orchestrator(
    root: Path, config: AICodingConfig, registry: HandlerRegistry
) -> Orchestrator:
    """Migrated database + orchestrator; closed automatically by :func:`close_all`."""
    configure_logging(json=True, level="WARNING")
    db = Database.for_project(root)
    upgrade_to_head(db)
    orch = Orchestrator(db=db, config=config, project_root=root, registry=registry)
    _OPENED.append(orch)
    return orch


def guard[T](fn: Callable[[], T]) -> T:
    """Map orchestrator errors to CLI exit codes."""
    try:
        return fn()
    except RunNotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    except (RunLeaseError, IdempotencyConflictError) as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.CONFLICT) from exc
    except (InvalidTransitionError, ApprovalError, OrchestratorError) as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.INVALID) from exc
