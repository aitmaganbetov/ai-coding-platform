"""Executor port and value objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_coding.domain.enums import ExecutionBackend
from ai_coding.domain.errors import PermanentError, TransientError

DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class ExecutorError(PermanentError):
    """The command cannot be executed as specified (invalid spec, denied binary…)."""


class ExecutorUnavailableError(TransientError):
    """The backend is not available right now (e.g. Docker daemon not running)."""


@dataclass(frozen=True, slots=True)
class Mount:
    """Host path made visible inside a sandbox (read-only unless stated)."""

    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """What to run. ``argv`` is never passed through a shell."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 1800.0
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    image: str | None = None  # sandbox image (digest-pinned) for container backends
    mounts: tuple[Mount, ...] = ()
    workdir: str | None = None  # working directory inside the sandbox
    network: bool = False  # sandbox network access (policy must allow it)
    # With ``network``: the only hosts the step may reach (egress allowlist proxy,
    # B-205); ``None`` means unrestricted (explicit operator actions only).
    egress: tuple[str, ...] | None = None
    # The listed hosts may resolve to private addresses (an internal staging system
    # named by the operator); never for registry hosts, where it would allow SSRF.
    egress_private: bool = False
    ok_exit_codes: frozenset[int] = frozenset({0})

    def __post_init__(self) -> None:
        if not self.argv or not all(isinstance(a, str) and a for a in self.argv):
            raise ExecutorError("argv must be a non-empty sequence of non-empty strings")
        if any("\0" in a for a in self.argv):
            raise ExecutorError("argv must not contain NUL bytes")
        if self.timeout_seconds <= 0:
            raise ExecutorError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of one command. Output is truncated and redacted."""

    backend: ExecutionBackend
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        """Finished in time with exit code 0."""
        return not self.timed_out and self.exit_code == 0


@runtime_checkable
class Executor(Protocol):
    """Port implemented by every execution backend."""

    backend: ExecutionBackend

    def available(self) -> bool:
        """Whether the backend can run commands right now."""
        ...

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        """Run ``spec`` to completion (or timeout)."""
        ...
