"""Windows Job Objects for the local executor (B-206). The structure is checked on every
platform; the limits themselves on the Windows CI runners (NFR-01: the CLI on Windows)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_coding.executors.process import ProcessOutcome, run_process
from ai_coding.executors.winjob import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_JOB_TIME,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobLimits,
    limit_information,
)

windows = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects exist only on Windows")

# In a virtual environment on Windows sys.executable is a launcher that starts the real
# interpreter as a child: every Python would be two processes in the job. The scripts here
# use only the standard library, so they run on the base interpreter (one process each).
PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def test_limit_information() -> None:
    info = limit_information(JobLimits(memory_mb=256, cpu_seconds=30, max_processes=8))
    flags = info.BasicLimitInformation.LimitFlags
    for flag in (JOB_OBJECT_LIMIT_JOB_MEMORY, JOB_OBJECT_LIMIT_JOB_TIME,
                 JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):  # fmt: skip
        assert flags & flag
    assert info.JobMemoryLimit == 256 * 1024 * 1024
    assert info.BasicLimitInformation.PerJobUserTimeLimit == 30 * 10_000_000
    assert info.BasicLimitInformation.ActiveProcessLimit == 8


async def _python(code: str, tmp_path: Path, limits: JobLimits) -> ProcessOutcome:
    return await run_process([PYTHON, "-c", code], cwd=tmp_path, env=dict(os.environ),
                             timeout=60, max_output_bytes=100_000, job=limits)  # fmt: skip


@windows
async def test_memory_of_the_tree_is_limited(tmp_path: Path) -> None:
    out = await _python("b = bytearray(512 * 1024 * 1024); print('allocated')", tmp_path,
                        JobLimits(memory_mb=128, cpu_seconds=60))  # fmt: skip
    assert out.exit_code != 0 and "allocated" not in out.stdout, out
    ok = await _python("b = bytearray(16 * 1024 * 1024); print('allocated')", tmp_path,
                       JobLimits(memory_mb=128, cpu_seconds=60))  # fmt: skip
    assert ok.exit_code == 0 and "allocated" in ok.stdout


@windows
async def test_process_count_is_limited(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys\n"
        "started = 0\n"
        "procs = []\n"
        "for _ in range(6):\n"
        "    try:\n"
        "        args = [sys.executable, '-c', 'import time; time.sleep(5)']\n"
        "        procs.append(subprocess.Popen(args))\n"
        "        started += 1\n"
        "    except OSError:\n"
        "        pass\n"
        "print('started', started)\n"
        "for p in procs: p.kill()\n"
    )
    out = await _python(code, tmp_path, JobLimits(memory_mb=512, cpu_seconds=60, max_processes=3))
    assert "started 2" in out.stdout, out  # parent + 2 children


@windows
async def test_nothing_outlives_the_command(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open(r'{pid_file}', 'w').write(str(p.pid))\n"
    )  # the parent exits at once and leaves a grandchild behind
    out = await _python(code, tmp_path, JobLimits(memory_mb=512, cpu_seconds=60))
    assert out.exit_code == 0, out
    pid = pid_file.read_text(encoding="utf-8").strip()
    listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True, check=False).stdout  # fmt: skip
    assert pid not in listing, listing  # the job closed: the grandchild was killed


def test_job_cpu_budget_covers_every_core() -> None:
    """JOB_TIME is the CPU time of all processes together: a command using 8 cores must
    not be killed after an eighth of its timeout (it used to get timeout + 5 s)."""
    from ai_coding.config.models import ResourceLimits  # noqa: PLC0415
    from ai_coding.executors.local import job_limits  # noqa: PLC0415

    limits = ResourceLimits(memory_mb=1024, timeout_seconds=600)
    assert job_limits(limits, 300.0, cpus=8).cpu_seconds == 300 * 8 + 5
    assert job_limits(limits, 300.0, cpus=1).cpu_seconds == 305
    assert job_limits(limits, 300.0, cpus=0).cpu_seconds == 305
    assert job_limits(limits, 0.5, cpus=3).memory_mb == 1024


async def test_command_is_killed_when_its_job_cannot_be_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the started process cannot be put into its Job Object, it must not keep running
    without limits and timeout: it is killed and the run fails."""
    import time  # noqa: PLC0415

    from ai_coding.executors import process  # noqa: PLC0415
    from ai_coding.executors.base import ExecutorError  # noqa: PLC0415

    class RefusingJob:
        closed = False

        def __init__(self, limits: JobLimits) -> None:
            pass

        def assign(self, pid: int) -> None:
            raise PermissionError("access denied")

        def close(self) -> None:
            RefusingJob.closed = True

    monkeypatch.setattr(process, "_WINDOWS", True)
    monkeypatch.setattr(process, "JobObject", RefusingJob)
    marker = tmp_path / "survived"
    code = f"import time; time.sleep(3); open(r'{marker}', 'w').write('x')"
    started = time.monotonic()
    with pytest.raises(ExecutorError, match="Job Object"):
        await run_process([sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ),
                          timeout=60, max_output_bytes=1000,
                          job=JobLimits(memory_mb=256, cpu_seconds=60))  # fmt: skip
    assert time.monotonic() - started < 3 and RefusingJob.closed
    await asyncio.sleep(4)
    assert not marker.exists()  # the command was killed, not left running
