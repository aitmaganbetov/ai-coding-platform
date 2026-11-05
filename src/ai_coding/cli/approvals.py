"""Human-in-the-loop for CLI runs: show pending approvals, decide, resume (ТЗ §5, AC-10).

Interactively (a terminal), the user approves or stops right away; the decision is
recorded with the OS user as actor and checked against the project roles like
``ai-coding run approve``. Without a terminal the run stays AWAITING_APPROVAL and the
commands to continue are printed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from ai_coding.cli.common import guard
from ai_coding.cli.console import info
from ai_coding.domain.enums import ApprovalStatus, RunState
from ai_coding.orchestrator import Orchestrator, RunStatus


def pending(status: RunStatus) -> list[dict[str, Any]]:
    """Pending approvals of a run."""
    return [a for a in status.approvals if a["status"] == ApprovalStatus.PENDING.value]


def settle(
    orch: Orchestrator,
    run_id: uuid.UUID,
    status: RunStatus,
    *,
    interactive: bool,
    project: Path,
    resume_hint: str,
    show: Callable[[list[dict[str, Any]]], None] | None = None,
) -> RunStatus:
    """Loop until the run no longer waits for a human (or the human stops)."""
    while status.state is RunState.AWAITING_APPROVAL:
        waiting = pending(status)
        for a in waiting:
            info(f"\napproval needed: [bold]{escape(a['operation'])}[/] — "
                 f"{escape(a['reason'] or '')}")  # fmt: skip
        if show is not None:
            show(waiting)
        if not interactive:
            info("\nRun is waiting for a decision:")
            info(f"  approve:  ai-coding run approve {run_id} -C {project}")
            info(f"  continue: {resume_hint}")
            return status
        choice = typer.prompt("Decision (approve/stop)", default="approve").strip().lower()
        if choice not in {"approve", "a", "y", "yes"}:
            info(f"stopped; continue later with: {resume_hint}")
            return status
        guard(lambda: orch.decide(run_id, approve=True, note="approved in CLI"))
        status = guard(lambda: asyncio.run(orch.resume(run_id)))
    return status
