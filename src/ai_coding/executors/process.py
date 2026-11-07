"""Subprocess runner shared by executors: bounded output, timeout, process-tree kill."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ai_coding.executors.base import ExecutorError
from ai_coding.executors.winjob import JobLimits, JobObject
from ai_coding.security import redact

_READ_CHUNK = 65536
_WINDOWS = os.name == "nt"


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Raw outcome of a process run."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


async def _drain(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read everything (so the child never blocks on a full pipe) but keep ``limit`` bytes."""
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return bytes(kept), truncated
        room = limit - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > max(room, 0):
            truncated = True


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it spawned."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        subprocess.run(  # noqa: S603 - fixed argv
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        return
    with contextlib.suppress(ProcessLookupError):  # already gone
        os.killpg(proc.pid, signal.SIGKILL)


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
    preexec: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    job: JobLimits | None = None,
) -> ProcessOutcome:
    """Run ``argv`` without a shell; kill the whole process group on timeout.

    ``job``: on Windows the process tree runs in a Job Object with these limits
    (memory, CPU time, process count; killed when the job closes). Ignored elsewhere,
    where ``preexec`` sets rlimits.
    """
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        if preexec is not None:
            kwargs["preexec_fn"] = preexec
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(  # argv list, never a shell
        *argv,
        cwd=str(cwd),
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,  # type: ignore[arg-type]
    )
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - PIPE requested
    try:
        windows_job = _job_for(proc, job)
    except OSError as exc:
        # The command is already running: never leave it without its limits and timeout.
        _kill_tree(proc)
        with contextlib.suppress(OSError):
            await asyncio.wait_for(proc.communicate(), timeout=30)
        raise ExecutorError(
            f"cannot place the command in a Windows Job Object (resource limits): {exc}"
        ) from exc
    try:
        return await _supervise(proc, timeout=timeout, max_output_bytes=max_output_bytes,
                                on_timeout=on_timeout, windows_job=windows_job,
                                started=started)  # fmt: skip
    finally:
        if windows_job is not None:  # pragma: no cover - Windows
            windows_job.close()  # KILL_ON_JOB_CLOSE: nothing outlives the command


def _job_for(proc: asyncio.subprocess.Process, limits: JobLimits | None) -> JobObject | None:
    """The Job Object holding ``proc`` (Windows), or None; OSError if it cannot be applied
    to a still-running process (the caller then kills it)."""
    if limits is None or not _WINDOWS:
        return None
    job = JobObject(limits)
    try:
        job.assign(proc.pid)
    except OSError:
        job.close()
        if proc.returncode is None:
            raise
        return None  # it already ended: nothing left to limit
    return job


async def _supervise(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    max_output_bytes: int,
    on_timeout: Callable[[], None] | None,
    windows_job: JobObject | None,
    started: float,
) -> ProcessOutcome:
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - PIPE requested

    def kill() -> None:
        if windows_job is not None:  # pragma: no cover - Windows
            windows_job.terminate()
        _kill_tree(proc)

    readers = asyncio.gather(
        _drain(proc.stdout, max_output_bytes), _drain(proc.stderr, max_output_bytes)
    )
    timed_out = False
    try:
        (out, out_trunc), (err, err_trunc) = await asyncio.wait_for(
            asyncio.shield(readers), timeout=timeout
        )
        await proc.wait()
    except TimeoutError:
        timed_out = True
        try:
            if on_timeout is not None:
                on_timeout()
        finally:  # the executor's own cleanup failing never keeps the process alive
            kill()
            (out, out_trunc), (err, err_trunc) = await readers
            await proc.wait()
    except asyncio.CancelledError:
        try:
            if on_timeout is not None:
                on_timeout()
        finally:
            kill()
        raise
    return ProcessOutcome(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=redact(out.decode("utf-8", errors="replace")),
        stderr=redact(err.decode("utf-8", errors="replace")),
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        stdout_truncated=out_trunc,
        stderr_truncated=err_trunc,
    )
