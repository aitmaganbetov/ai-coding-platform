"""Local Executor: restricted host execution (ТЗ §4: "Local (ограниченный)").

Restrictions:

* only executables in ``allowed_executables`` may run (deny by default);
* the executable is resolved to an absolute path via ``PATH`` before running;
* argv only, never a shell; stdin is closed;
* environment is rebuilt from an allowlist (no inherited secrets);
* POSIX resource limits: CPU time, file size, open files, no core dumps
  (memory limits need cgroups — use the Docker sandbox);
* the whole process group is killed on timeout or cancellation;
* output is size-bounded and redacted.

Whether a command may run on the host at all is a policy decision taken by the
caller (Policy Engine) before reaching the executor.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from ai_coding.config.models import ResourceLimits
from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors.base import CommandSpec, ExecutionResult, ExecutorError
from ai_coding.executors.process import run_process

ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP",
    "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "XDG_CACHE_HOME",
)  # fmt: skip


def _limits_preexec(limits: ResourceLimits, timeout: float) -> Callable[[], None] | None:
    if sys.platform == "win32":  # pragma: no cover - no rlimits on Windows (Job Objects: S9)
        return None
    import resource  # noqa: PLC0415 - POSIX-only module

    cpu = int(timeout) + 5
    max_file = limits.memory_mb * 1024 * 1024

    def apply() -> None:  # runs in the child between fork and exec
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file, max_file))
        # No RLIMIT_AS: it caps *virtual* address space, which Go, JVM and Node
        # runtimes reserve far beyond real use (gitleaks crashes at start-up).
        # Real memory limits need cgroups, i.e. the Docker sandbox (ADR-0018).

    return apply


class LocalExecutor:
    """Runs allow-listed executables on the host with limits."""

    backend = ExecutionBackend.LOCAL

    def __init__(
        self,
        *,
        allowed_executables: Iterable[str],
        limits: ResourceLimits | None = None,
        env_allowlist: Iterable[str] = ENV_ALLOWLIST,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self._allowed = frozenset(allowed_executables)
        self._limits = limits or ResourceLimits()
        self._env_allowlist = tuple(env_allowlist)
        self._base_env = base_env if base_env is not None else os.environ

    def available(self) -> bool:
        """The local backend is always available."""
        return True

    def resolve(self, executable: str) -> str:
        """Absolute path of an allow-listed executable."""
        name = Path(executable).name
        stem = name.removesuffix(".exe").removesuffix(".EXE")
        if stem not in self._allowed and name not in self._allowed:
            raise ExecutorError(f"executable '{name}' is not allowed on the local executor")
        if Path(executable).is_absolute():
            if not Path(executable).is_file():
                raise ExecutorError(f"executable '{executable}' not found")
            return executable
        found = shutil.which(executable, path=self._base_env.get("PATH"))
        if found is None:
            raise ExecutorError(f"executable '{executable}' not found on PATH")
        return found

    def _env(self, extra: Mapping[str, str]) -> dict[str, str]:
        env = {k: v for k, v in self._base_env.items() if k in self._env_allowlist}
        env.update(extra)
        return env

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        """Run ``spec`` on the host."""
        if spec.image is not None or spec.mounts:
            raise ExecutorError("image/mounts are sandbox options; use the Docker executor")
        if spec.network:
            raise ExecutorError("the local executor cannot grant network isolation guarantees")
        if not spec.cwd.is_dir():
            raise ExecutorError(f"working directory {spec.cwd} does not exist")
        argv = (self.resolve(spec.argv[0]), *spec.argv[1:])
        outcome = await run_process(
            argv,
            cwd=spec.cwd,
            env=self._env(spec.env),
            timeout=min(spec.timeout_seconds, float(self._limits.timeout_seconds)),
            max_output_bytes=spec.max_output_bytes,
            preexec=_limits_preexec(self._limits, spec.timeout_seconds),
        )
        return ExecutionResult(
            backend=self.backend,
            argv=argv,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_seconds=outcome.duration_seconds,
            timed_out=outcome.timed_out,
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
        )
