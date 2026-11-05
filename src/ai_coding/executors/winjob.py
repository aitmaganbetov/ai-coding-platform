"""Windows Job Objects: resource limits for the local executor on Windows (B-206).

POSIX gets rlimits in the child between fork and exec; Windows has no equivalent per
process, but a Job Object limits a whole process tree:

* ``JOB_OBJECT_LIMIT_JOB_MEMORY`` — committed memory of all processes together (stronger
  than the POSIX limits, which cannot bound memory without cgroups);
* ``JOB_OBJECT_LIMIT_JOB_TIME`` — total user-mode CPU time;
* ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` — no fork bombs;
* ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — closing the job (timeout, cancellation, the
  platform exiting) ends every process in it, so nothing is left running.

The process is assigned right after it starts; a child it spawns before that instant is
outside the job (asyncio cannot create a suspended process). Use the Docker sandbox for
untrusted code — the local executor runs only allow-listed host commands (ADR-0018).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _Basic(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _Io(ctypes.Structure):
    _fields_ = tuple((name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    ))  # fmt: skip


class _Extended(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _Basic),
        ("IoInfo", _Io),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


@dataclass(frozen=True, slots=True)
class JobLimits:
    """Limits of one command's process tree."""

    memory_mb: int
    cpu_seconds: int
    max_processes: int = 256


def limit_information(limits: JobLimits) -> _Extended:
    """The structure passed to ``SetInformationJobObject`` (platform-independent)."""
    info = _Extended()
    basic = info.BasicLimitInformation
    basic.LimitFlags = (JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_JOB_TIME
                        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)  # fmt: skip
    basic.PerJobUserTimeLimit = limits.cpu_seconds * 10_000_000  # 100-ns units
    basic.ActiveProcessLimit = limits.max_processes
    info.JobMemoryLimit = limits.memory_mb * 1024 * 1024
    return info


class JobObject:  # pragma: no cover - exercised on the Windows CI runners
    """A Job Object holding one command's process tree."""

    def __init__(self, limits: JobLimits) -> None:
        if os.name != "nt":
            raise OSError("Job Objects exist only on Windows")
        self._k32: Any = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._handle: int | None = self._k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise _last_error()
        info = limit_information(limits)
        if not self._k32.SetInformationJobObject(
            self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        ):  # fmt: skip
            error = _last_error()
            self.close()
            raise error

    def assign(self, pid: int) -> None:
        process = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise _last_error()
        try:
            if not self._k32.AssignProcessToJobObject(self._handle, process):
                raise _last_error()
        finally:
            self._k32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle:
            self._k32.TerminateJobObject(self._handle, exit_code)

    def close(self) -> None:
        """Close the job; KILL_ON_JOB_CLOSE ends any process still in it."""
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def _last_error() -> OSError:  # pragma: no cover - Windows
    error: OSError = getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())  # noqa: B009
    return error
