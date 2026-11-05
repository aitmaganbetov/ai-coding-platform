"""Project tools: analysers that run the project's own toolchain (ADR-0027).

PHPStan, Pint, ESLint, ``tsc``, mypy, ``go test``, golangci-lint, ``cargo test`` and
Clippy load the project's dependencies (``vendor/``, ``node_modules/``, modules,
crates) and, for tests, execute project code. They therefore run **only** in the
Docker sandbox (never on the host) and follow the same rules as sandboxed tests:

* the project is mounted **read-only** at ``/work``; when dependencies must be
  installed first, a scratch *copy* is used instead, so the repository is never
  written (FR-06);
* ``/deps`` is a writable scratch volume for caches, module stores and build output
  (``/tmp`` is ``noexec``);
* the optional install step is the only networked command and runs only when
  ``execution.docker.network`` is ``bridge`` **and** the egress policy allows every
  host it needs; otherwise the tool is SKIPPED with the reason;
* a tool that does not apply (no manifest, no config, no dependency) is SKIPPED, a
  tool that ran but could not produce a result fails its gate (fail-closed).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors.base import (
    CommandSpec,
    ExecutionResult,
    Executor,
    ExecutorError,
    Mount,
)
from ai_coding.security import redact
from ai_coding.tools.base import Finding, ToolRun, ToolSpec, skipped_run
from ai_coding.tools.cache import tool_cache
from ai_coding.tools.scratch import sandbox_scratch
from ai_coding.tools.testrun import copy_tree, open_permissions

WORK = "/work"
DEPS = "/deps"
TOOLCHAIN = "/toolchain"
_COPY_SKIP = frozenset({".git", ".ai-coding", "node_modules", "vendor", ".venv", "venv",
                        "target", "__pycache__", ".mypy_cache", ".pytest_cache"})  # fmt: skip
_MAX_COPY = 500 * 1024 * 1024
_ERROR_CHARS = 400


@dataclass(frozen=True, slots=True)
class Install:
    """Dependency installation needed before the tool can run."""

    argv: tuple[str, ...]
    hosts: tuple[str, ...]
    writes_project: bool = True  # installs into the project tree (vendor/, node_modules/)


@dataclass(frozen=True, slots=True)
class Invocation:
    """How to run the tool for a given project."""

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    install: Install | None = None
    version_argv: tuple[str, ...] | None = None
    writable: bool = False  # the tool writes into the tree (e.g. Cargo.lock): use a copy
    toolchain: str | None = None  # platform-cache entry mounted at /toolchain


@runtime_checkable
class ProjectTool(Protocol):
    """Contract of a project tool."""

    spec: ToolSpec

    def plan(self, root: Path) -> Invocation | str:
        """How to run on ``root``, or the reason the tool does not apply."""
        ...

    def ok_exit_codes(self) -> frozenset[int]:
        """Exit codes that mean "the tool produced a result" (with or without issues)."""
        ...

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        """Findings with ``/work``-relative paths."""
        ...

    def parse_version(self, output: str) -> str | None:
        """Version string from the version command output."""
        ...


def _short(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tail = " | ".join(lines[-3:])
    return tail if len(tail) <= _ERROR_CHARS else "…" + tail[-_ERROR_CHARS:]


class ProjectToolRunner:
    """Runs :class:`ProjectTool` adapters in the Docker sandbox."""

    def __init__(
        self,
        executor: Executor,
        project_root: Path,
        *,
        network_allowed: Callable[[str], bool],
        sandbox_network: bool,
        timeout_seconds: float = 1800.0,
    ) -> None:
        self.executor = executor
        self.root = project_root.resolve()
        self._network_allowed = network_allowed
        self._sandbox_network = sandbox_network
        self.timeout = timeout_seconds

    def _install_blocker(self, install: Install) -> str | None:
        if not self._sandbox_network:
            return ("dependencies must be installed first, but execution.docker.network "
                    "is 'none'")  # fmt: skip
        denied = [h for h in install.hosts if not self._network_allowed(h)]
        if denied:
            return f"dependency install needs egress to {', '.join(denied)} (security policy)"
        return None

    def _plan(self, tool: ProjectTool) -> Invocation | str:
        """The invocation, or why the tool is skipped."""
        if self.executor.backend is not ExecutionBackend.DOCKER:
            return "runs project code: Docker sandbox only"
        plan = tool.plan(self.root)
        if isinstance(plan, str):
            return plan
        blocker = self._install_blocker(plan.install) if plan.install is not None else None
        if blocker:
            return blocker
        if plan.toolchain is not None and not tool_cache(plan.toolchain).is_dir():
            return (f"toolchain '{plan.toolchain}' is not prepared "
                    f"(run `ai-coding tools pull -t {tool.spec.name}`)")  # fmt: skip
        return plan

    async def run(self, tool: ProjectTool) -> ToolRun:
        """Run ``tool`` against the project."""
        spec = tool.spec
        backend = self.executor.backend.value
        plan = self._plan(tool)
        if isinstance(plan, str):
            return skipped_run(spec, backend, plan)
        base = ToolRun(tool=spec.name, category=spec.category, version=None, backend=backend,
                       exit_code=-1, succeeded=False, findings=(),
                       duration_seconds=0.0)  # fmt: skip
        started = time.monotonic()
        try:
            outcome = await self._execute(tool, plan)
        except ExecutorError as exc:
            return replace(base, error=str(exc)[:_ERROR_CHARS])
        duration = round(time.monotonic() - started, 3)
        if isinstance(outcome, str):
            return replace(base, duration_seconds=duration, error=outcome)
        version, result = outcome
        return self._finish(tool, replace(base, version=version, duration_seconds=duration),
                            result)  # fmt: skip

    async def _execute(
        self, tool: ProjectTool, plan: Invocation
    ) -> tuple[str | None, ExecutionResult] | str:
        """Install (if needed), read the version, run; an error message on install failure."""
        spec = tool.spec
        async with sandbox_scratch(self.executor, f"ai-coding-{spec.name}-") as scratch:
            tmp = scratch.path
            scratch.used(spec.image)
            deps = Path(tmp) / "deps"
            deps.mkdir()
            if plan.writable or (plan.install is not None and plan.install.writes_project):
                work, work_ro = Path(tmp) / "work", False
                copy_tree(self.root, work, skip=_COPY_SKIP, max_bytes=_MAX_COPY)
            else:
                work, work_ro = self.root, True
            open_permissions(Path(tmp))
            mounts: tuple[Mount, ...] = (Mount(work, WORK, read_only=work_ro),
                                         Mount(deps, DEPS, read_only=False))  # fmt: skip
            if plan.toolchain is not None:
                mounts += (Mount(tool_cache(plan.toolchain), TOOLCHAIN, read_only=False),)

            def command(
                argv: tuple[str, ...], *, egress: tuple[str, ...] | None = None
            ) -> CommandSpec:
                return CommandSpec(
                    argv=argv, cwd=work, image=spec.image, mounts=mounts, workdir=WORK,
                    network=egress is not None, egress=egress,
                    env={**dict(spec.env), **dict(plan.env)},
                    timeout_seconds=self.timeout, ok_exit_codes=frozenset(range(256)),
                )  # fmt: skip

            if plan.install is not None:  # only the install hosts are reachable (B-205)
                inst = await self.executor.run(
                    command(plan.install.argv, egress=tuple(plan.install.hosts))
                )
                if inst.exit_code != 0:
                    return "dependency install failed: " + _short(inst.stderr or inst.stdout)
            version = None
            if plan.version_argv:
                ver = await self.executor.run(command(plan.version_argv))
                if ver.exit_code == 0:
                    version = tool.parse_version(ver.stdout + "\n" + ver.stderr)
            return version, await self.executor.run(command(plan.argv))

    @staticmethod
    def _finish(tool: ProjectTool, base: ToolRun, result: ExecutionResult) -> ToolRun:
        base = replace(base, exit_code=result.exit_code, raw_output=redact(result.stdout))
        if result.timed_out:
            return replace(base, error="timed out")
        if result.stdout_truncated:
            return replace(base, error="output exceeded the size limit; result incomplete")
        if result.exit_code not in tool.ok_exit_codes():
            return replace(base, error=f"exit code {result.exit_code}: "
                           f"{_short(result.stderr or result.stdout)}")  # fmt: skip
        try:
            findings = tool.parse(result.stdout, result.stderr, result.exit_code)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return replace(base, error=f"could not parse output: {exc}"[:_ERROR_CHARS])
        coverage_of = getattr(tool, "coverage", None)
        coverage = coverage_of(result.stdout, result.stderr) if coverage_of else None
        return replace(base, succeeded=True, findings=tuple(findings), coverage=coverage)


def has_any(root: Path, names: Sequence[str]) -> bool:
    """Whether any of ``names`` exists under ``root``."""
    return any((root / n).exists() for n in names)
