"""Scratch directories shared with the sandbox, and their removal.

The sandbox runs as an unprivileged user (uid 1000) that usually differs from the
host user running the platform. What it writes into a scratch directory belongs to
that user, sometimes with modes the host cannot override: Go's module cache is
read-only by design, npm and cargo create private directories. Deleting such a tree
on the host fails with ``PermissionError`` (and would abort the audit or test run).

The fallback removes the entries as the sandbox user, in an image the run already
used, without network; only then the host removes what is left.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ai_coding.executors.base import CommandSpec, Executor, ExecutorError, Mount
from ai_coding.observability import get_logger

SCRATCH = "/scratch"
# Make each directory writable before descending into it (``chmod -R`` in BusyBox stops
# at the first entry it cannot change, e.g. a host-owned directory), then remove.
_CLEAN = (
    'fix() { for p in "$1"/* "$1"/.[!.]*; do [ -d "$p" ] && [ ! -L "$p" ] || continue; '
    'chmod u+rwx "$p" 2>/dev/null; fix "$p"; done; }; '
    'fix "$1"; for p in "$1"/* "$1"/.[!.]*; do [ -e "$p" ] || [ -L "$p" ] || continue; '
    'rm -rf "$p"; done'
)

log = get_logger(__name__)


def clean_argv(target: str = SCRATCH) -> tuple[str, ...]:
    """Remove every entry under ``target`` (not ``target`` itself), fixing modes first."""
    return ("sh", "-c", _CLEAN, "sh", target)


@dataclass
class Scratch:
    """A scratch directory and the images that wrote into it."""

    path: Path
    images: list[str] = field(default_factory=list)

    def used(self, image: str) -> str:
        """Remember ``image`` as one that can clean up after itself; returns it."""
        if image not in self.images:
            self.images.append(image)
        return image


async def remove_scratch(executor: Executor, scratch: Scratch) -> bool:
    """Delete ``scratch.path``; ``False`` (logged, never raised) if something remains."""
    try:
        shutil.rmtree(scratch.path)
    except FileNotFoundError:
        return True
    except PermissionError:
        pass
    else:
        return True
    for image in scratch.images:
        try:
            await executor.run(CommandSpec(
                argv=clean_argv(), cwd=scratch.path, image=image, workdir=SCRATCH,
                mounts=(Mount(scratch.path, SCRATCH, read_only=False),), network=False,
                timeout_seconds=300.0, ok_exit_codes=frozenset(range(256)),
            ))  # fmt: skip
        except ExecutorError as exc:
            log.warning("scratch.cleanup_failed", path=str(scratch.path), error=str(exc))
            continue
        shutil.rmtree(scratch.path, ignore_errors=True)
        if not scratch.path.exists():
            return True
    shutil.rmtree(scratch.path, ignore_errors=True)
    if scratch.path.exists():
        log.warning("scratch.left_behind", path=str(scratch.path))
        return False
    return True


@asynccontextmanager
async def sandbox_scratch(executor: Executor, prefix: str) -> AsyncIterator[Scratch]:
    """A temporary directory for the sandbox, removed afterwards even when the sandbox
    user wrote files the host user cannot delete."""
    scratch = Scratch(Path(tempfile.mkdtemp(prefix=prefix)))
    try:
        yield scratch
    finally:
        await remove_scratch(executor, scratch)
