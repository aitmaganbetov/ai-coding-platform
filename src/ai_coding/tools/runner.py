"""Runs a tool adapter through an executor, read-only, and normalises the result."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors.base import (
    CommandSpec,
    ExecutionResult,
    Executor,
    ExecutorError,
    Mount,
)
from ai_coding.tools.base import ToolAdapter, ToolRun, skipped_run
from ai_coding.tools.cache import tool_cache
from ai_coding.tools.project import ProjectTool
from ai_coding.tools.semgrep import SemgrepAdapter, bundled_rules_dir

SANDBOX_SRC = "/src"
VERSION_TIMEOUT_SECONDS = 60.0
SANDBOX_RULES = "/rules"
SANDBOX_CACHE = "/cache"
_MAX_ERROR_CHARS = 400


def _short(text: str) -> str:
    """Last meaningful lines of tool output, bounded (reports and gate reasons)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tail = " | ".join(lines[-3:])
    return tail if len(tail) <= _MAX_ERROR_CHARS else "…" + tail[-_MAX_ERROR_CHARS:]


class ToolRunner:
    """Executes adapters against a project directory with a given executor."""

    def __init__(
        self,
        executor: Executor,
        project_root: Path,
        *,
        timeout_seconds: float = 900.0,
        semgrep_config: Path | None = None,
    ) -> None:
        self.executor = executor
        self.root = project_root.resolve()
        self.timeout = timeout_seconds
        self.semgrep_config = semgrep_config
        self._versions: dict[str, str | None] = {}

    @property
    def sandboxed(self) -> bool:
        """Whether commands run in a container."""
        return self.executor.backend is ExecutionBackend.DOCKER

    def available(self, adapter: ToolAdapter | ProjectTool) -> tuple[bool, str]:
        """Whether ``adapter`` can run here, with a human-readable reason."""
        if self.sandboxed:
            ok = self.executor.available()
            return ok, "docker daemon reachable" if ok else "docker daemon not reachable"
        found = shutil.which(adapter.spec.binary)
        return (found is not None, found or f"'{adapter.spec.binary}' not found on PATH")

    def _spec(
        self,
        adapter: ToolAdapter,
        args: list[str],
        *,
        target: str,
        mounts: tuple[Mount, ...],
        timeout: float | None = None,
    ) -> CommandSpec:
        env = dict(adapter.spec.env)
        scan_env = getattr(adapter, "scan_env", None)
        if scan_env is not None:
            _, extra, _ = self._target_and_mounts(adapter)
            env.update(scan_env(extra))
        seconds = timeout or self.timeout
        if self.sandboxed:
            return CommandSpec(
                argv=(adapter.spec.binary, *args),
                cwd=self.root,
                env=env,
                image=adapter.spec.image,
                mounts=mounts,
                workdir=target,
                timeout_seconds=seconds,
            )
        return CommandSpec(
            argv=(adapter.spec.binary, *args), cwd=self.root, env=env, timeout_seconds=seconds
        )

    def _target_and_mounts(
        self, adapter: ToolAdapter
    ) -> tuple[str, dict[str, str], tuple[Mount, ...]]:
        extra: dict[str, str] = {}
        cache = self._cache(adapter)
        if self.sandboxed:
            mounts = [Mount(self.root, SANDBOX_SRC, read_only=True)]
            if cache is not None:
                # Platform cache (not the project): the DB engine needs write access.
                mounts.append(Mount(cache, SANDBOX_CACHE, read_only=False))
                extra["cache"] = SANDBOX_CACHE
            if isinstance(adapter, SemgrepAdapter):
                config = self.semgrep_config or (bundled_rules_dir() / "ai-coding-baseline.yml")
                mounts.append(Mount(config.parent, SANDBOX_RULES, read_only=True))
                extra["config"] = f"{SANDBOX_RULES}/{config.name}"
            return SANDBOX_SRC, extra, tuple(mounts)
        if isinstance(adapter, SemgrepAdapter):
            config = self.semgrep_config or (bundled_rules_dir() / "ai-coding-baseline.yml")
            extra["config"] = str(config)
        if cache is not None:
            extra["cache"] = str(cache)
        return str(self.root), extra, ()

    @staticmethod
    def _cache(adapter: ToolAdapter) -> Path | None:
        name = getattr(adapter, "cache_name", None)
        return tool_cache(name) if name else None

    async def version(self, adapter: ToolAdapter) -> str | None:
        """Tool version (cached), recorded in reports for reproducibility (NFR-05)."""
        name = adapter.spec.name
        if name not in self._versions:
            target, _, _ = self._target_and_mounts(adapter)
            spec = self._spec(
                adapter,
                list(adapter.version_args()),
                target=target,
                mounts=(),
                timeout=VERSION_TIMEOUT_SECONDS,
            )
            try:
                result = await self.executor.run(spec)
                text = result.stdout + "\n" + result.stderr
                self._versions[name] = (
                    adapter.parse_version(text) if result.exit_code == 0 else None
                )
            except ExecutorError:
                self._versions[name] = None
        return self._versions[name]

    async def run(self, adapter: ToolAdapter) -> ToolRun:
        """Scan the project with ``adapter``."""
        cache = self._cache(adapter)
        metadata: dict[str, Any] = {}
        if cache is not None:
            ready = getattr(adapter, "cache_ready", None)
            if not cache.is_dir() or (ready is not None and not ready(cache)):
                return skipped_run(
                    adapter.spec, self.executor.backend.value,
                    f"offline database missing in {cache} (run `ai-coding tools pull "
                    f"-t {adapter.spec.name}`)",
                )  # fmt: skip
            describe = getattr(adapter, "cache_metadata", None)
            metadata = dict(describe(cache)) if describe is not None else {}
        target, extra, mounts = self._target_and_mounts(adapter)
        version = await self.version(adapter)
        spec = self._spec(
            adapter, list(adapter.scan_args(target, extra=extra)), target=target, mounts=mounts
        )
        result = await self.executor.run(spec)
        run = self._to_run(adapter, result, version, target)
        return replace(run, metadata=metadata) if metadata else run

    def _to_run(
        self, adapter: ToolAdapter, result: ExecutionResult, version: str | None, target: str
    ) -> ToolRun:
        base = ToolRun(
            tool=adapter.spec.name,
            category=adapter.spec.category,
            version=version,
            backend=result.backend.value,
            exit_code=result.exit_code,
            succeeded=False,
            findings=(),
            duration_seconds=result.duration_seconds,
            raw_output=result.stdout,
        )
        if result.timed_out:
            return replace(base, error="timed out")
        if result.stdout_truncated:
            return replace(base, error="output exceeded the size limit; result incomplete")
        if result.exit_code not in adapter.ok_exit_codes():
            return replace(
                base,
                error=f"exit code {result.exit_code}: {_short(result.stderr or result.stdout)}",
            )
        try:
            findings = adapter.parse(result.stdout, target=target)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return replace(base, error=f"could not parse output: {exc}"[:_MAX_ERROR_CHARS])
        return replace(base, succeeded=True, findings=tuple(findings))
