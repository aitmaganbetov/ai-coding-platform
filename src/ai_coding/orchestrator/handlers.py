"""Built-in deterministic handlers for the DISCOVERY phase (FR-01, FR-02).

They only *read* the project: configuration, git state, stacks and the context
index. They never execute repository code and need no LLM, so a discovery run
works fully offline (FR-12).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from ai_coding.config.loader import find_project_config
from ai_coding.config.validator import validate_file
from ai_coding.detection import detect_stacks
from ai_coding.domain.enums import TaskPhase
from ai_coding.git import GitError, LocalGitRepository, git_available
from ai_coding.orchestrator.context import ContextManager
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.security import redact


class _Builtin:
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True


class ConfigDiscoveryHandler(_Builtin):
    """Validates ``.ai-coding.yml`` of the project."""

    name = "builtin.discovery.config"
    capabilities = frozenset({"discovery.config"})

    async def run(self, ctx: StepContext) -> StepResult:
        path = find_project_config(ctx.project_root)
        if path is None:
            raise PermanentError("configuration file not found")
        report = await asyncio.to_thread(validate_file, path)
        if not report.ok:
            raise PermanentError(f"configuration is invalid ({len(report.issues)} issue(s))")
        cfg = report.config
        assert cfg is not None  # noqa: S101 - report.ok implies config
        return StepResult(
            output={
                "path": path.name,
                "valid": True,
                "schema_version": cfg.schema_version,
                "project": cfg.project.name,
                "mode": cfg.mode.value,
                "backend": cfg.execution.backend.value,
                "network": cfg.security.network.value,
                "ai_enabled": cfg.ai.enabled,
            },
            evidence_refs=[f"file:{path.name}"],
        )


class GitDiscoveryHandler(_Builtin):
    """Reports access and state of the git repository (FR-01)."""

    name = "builtin.discovery.git"
    capabilities = frozenset({"discovery.git"})

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(output=await asyncio.to_thread(self._inspect, ctx))

    @staticmethod
    def _inspect(ctx: StepContext) -> dict[str, Any]:
        if not git_available():
            return {"is_repository": False, "reason": "git not installed"}
        if not LocalGitRepository.is_repository(ctx.tree):
            return {"is_repository": False, "reason": "not a git working tree"}
        try:
            repo = LocalGitRepository.open(
                ctx.tree, protected_branches=ctx.config.project.protected_branches
            )
            status = repo.status()
            remote = repo.remote_url()
        except GitError as exc:
            raise PermanentError(f"git inspection failed: {exc}") from exc
        return {
            "is_repository": True,
            "branch": status.branch,
            "detached": status.is_detached,
            "head": status.head,
            "clean": status.is_clean,
            "changed_files": len(status.files),
            "untracked_files": sum(1 for f in status.files if f.is_untracked),
            "remote": redact(remote) if remote else None,
            "on_protected_branch": bool(status.branch and repo.is_protected(status.branch)),
        }


class StackDiscoveryHandler(_Builtin):
    """Detects stacks and compares them with the configured ones (FR-02)."""

    name = "builtin.discovery.stacks"
    capabilities = frozenset({"discovery.stacks"})

    async def run(self, ctx: StepContext) -> StepResult:
        evidence = await asyncio.to_thread(detect_stacks, ctx.tree)
        detected = [e.stack.value for e in evidence]
        configured = [s.value for s in ctx.config.stacks]
        return StepResult(
            output={
                "detected": detected,
                "configured": configured,
                "effective": configured or detected,
                "missing_in_config": sorted(set(detected) - set(configured)) if configured else [],
                "evidence": {e.stack.value: e.path for e in evidence},
            },
            evidence_refs=[f"file:{e.path}" for e in evidence],
        )


class ContextIndexHandler(_Builtin):
    """Builds the context index (excluding secret-bearing files)."""

    name = "builtin.discovery.context"
    capabilities = frozenset({"discovery.context"})

    async def run(self, ctx: StepContext) -> StepResult:
        manager = ContextManager(ctx.tree)
        index = await asyncio.to_thread(manager.build_index)
        extensions = Counter(PurePosixPath(p).suffix or "<none>" for p in index)
        return StepResult(
            output={
                "indexed_files": len(index),
                "indexed_bytes": sum(f.size_bytes for f in index.values()),
                "excluded_sensitive_or_vendored": manager.excluded_secret_paths,
                "top_extensions": dict(extensions.most_common(10)),
            }
        )


class DiscoveryReportHandler(_Builtin):
    """Verification step: assembles the discovery report artifact."""

    name = "builtin.report.discovery"
    capabilities = frozenset({"report.discovery"})

    async def run(self, ctx: StepContext) -> StepResult:
        report = {"run_id": str(ctx.run_id), "sections": dict(sorted(ctx.dependencies.items()))}
        data = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ref = await asyncio.to_thread(
            ctx.save_artifact, "discovery-report.json", data, "application/json"
        )
        return StepResult(
            output={"artifact": ref.uri, "sections": sorted(ctx.dependencies)},
            evidence_refs=[ref.uri],
        )


def builtin_registry() -> HandlerRegistry:
    """Registry with all built-in handlers plus installed plugins."""
    registry = HandlerRegistry(
        [
            ConfigDiscoveryHandler(),
            GitDiscoveryHandler(),
            StackDiscoveryHandler(),
            ContextIndexHandler(),
            DiscoveryReportHandler(),
        ]
    )
    registry.load_entry_points()
    return registry


_REPORT_CRITERIA = {
    "type": "object",
    "required": ["artifact", "sections"],
    "properties": {
        "artifact": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "sections": {"type": "array", "minItems": 4},
    },
}


def discovery_plan(*, max_attempts: int = 1, timeout_seconds: float = 300.0) -> list[StepSpec]:
    """Steps of a discovery run: four parallel probes and a report."""
    probes = ("config", "git", "stacks", "context")
    steps = [
        StepSpec(
            key=f"discover.{name}",
            name=f"Discover {name}",
            capability=f"discovery.{name}",
            phase=TaskPhase.DISCOVERY,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        for name in probes
    ]
    steps.append(
        StepSpec(
            key="report.discovery",
            name="Discovery report",
            capability="report.discovery",
            phase=TaskPhase.VERIFICATION,
            depends_on=tuple(f"discover.{n}" for n in probes),
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            success_criteria=_REPORT_CRITERIA,
        )
    )
    return steps
