"""Shared Rich consoles. All human output goes through the redactor."""

from __future__ import annotations

from rich.console import Console

from ai_coding.security import redact

stdout = Console(highlight=False, soft_wrap=True)
stderr = Console(stderr=True, highlight=False, soft_wrap=True)


def info(message: str) -> None:
    """Print an informational line to stdout."""
    stdout.print(redact(message))


def error(message: str) -> None:
    """Print an error line to stderr."""
    stderr.print(f"[bold red]error:[/] {redact(message)}")
