"""AUDIT workflow: read-only, deterministic, offline-capable (FR-06, ТЗ §16 S3).

DAG::

    discovery:    discover.config  discover.git  discover.stacks  audit.snapshot
    execution:    scan.<tool> …  (gitleaks, semgrep, ruff — by stack)
    verification: quality.gates   audit.integrity   →   report.audit

``audit.integrity`` proves the audit did not modify the repository: it compares
a snapshot taken before the scans with the state afterwards and fails the run if
anything changed (read-only by default, FR-06).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.config.models import AICodingConfig
from ai_coding.detection import detect_stacks
from ai_coding.domain.enums import ExecutionBackend, Stack, TaskPhase
from ai_coding.executors import DockerSandboxExecutor, LocalExecutor
from ai_coding.executors.base import Executor, ExecutorError, ExecutorUnavailableError
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.handlers import (
    ConfigDiscoveryHandler,
    GitDiscoveryHandler,
    StackDiscoveryHandler,
)
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.persistence.types import utcnow
from ai_coding.quality import QualityGateEngine, load_baseline
from ai_coding.quality.baseline import BaselineError
from ai_coding.reports import AuditReport, render_markdown
from ai_coding.tools import ToolRun, ToolRunner, default_tool_registry
from ai_coding.tools.base import Finding, skipped_run
from ai_coding.tools.project import ProjectTool, ProjectToolRunner
from ai_coding.tools.registry import AnyTool

_SNAPSHOT_EXCLUDES = {".git", ".ai-coding"}


@dataclass(frozen=True, slots=True)
class AuditOptions:
    """User-selectable options of an audit run."""

    backend: ExecutionBackend
    tools: tuple[str, ...] = ()  # empty: every tool applicable to the stacks
    semgrep_config: Path | None = None
    strict: bool = False


def snapshot_digest(root: Path) -> tuple[str, int]:
    """Digest of the working tree metadata (path, size, mtime) excluding VCS/state dirs."""
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SNAPSHOT_EXCLUDES)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                st = path.lstat()
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            entries.append(f"{rel}\x1f{st.st_size}\x1f{st.st_mtime_ns}\x1f{st.st_mode}")
    digest = hashlib.sha256("\n".join(entries).encode("utf-8", "surrogateescape")).hexdigest()
    return digest, len(entries)


# ------------------------------------------------------------------ handlers --
class _Handler:
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True


class SnapshotHandler(_Handler):
    """Records the pre-audit state of the working tree."""

    name = "builtin.audit.snapshot"
    capabilities = frozenset({"audit.snapshot"})

    async def run(self, ctx: StepContext) -> StepResult:
        digest, files = await asyncio.to_thread(snapshot_digest, ctx.project_root)
        return StepResult(output={"digest": digest, "files": files})


class ToolScanHandler(_Handler):
    """Runs one analysis tool through the selected executor."""

    def __init__(self, adapter: AnyTool, runner: ToolRunner) -> None:
        self.adapter = adapter
        self.runner = runner
        self.name = f"tool.{adapter.spec.name}"
        self.capabilities = frozenset({f"scan.{adapter.spec.name}"})

    async def run(self, ctx: StepContext) -> StepResult:
        spec = self.adapter.spec
        backend = self.runner.executor.backend
        if isinstance(self.adapter, ProjectTool) and backend is not ExecutionBackend.DOCKER:
            tool_run = skipped_run(spec, backend.value,
                                   "runs the project's toolchain: Docker sandbox only")  # fmt: skip
        else:
            ctx.authorize(ctx.policy.tool(spec.name,
                                          executes_project_code=spec.executes_project_code,
                                          backend=backend))  # fmt: skip
            tool_run = await self._scan(ctx)
        ctx.record_findings(tool_run.findings)
        evidence: list[str] = []
        if tool_run.raw_output:
            ref = ctx.save_artifact(
                f"{spec.name}-output.json", tool_run.raw_output.encode("utf-8"), "application/json"
            )
            evidence.append(ref.uri)
        return StepResult(output=tool_run.to_dict(), evidence_refs=evidence)

    async def _scan(self, ctx: StepContext) -> ToolRun:
        available, reason = await asyncio.to_thread(self.runner.available, self.adapter)
        if not available:
            return self._failed(f"unavailable: {reason}")
        try:
            if isinstance(self.adapter, ProjectTool):
                docker = ctx.config.execution.docker
                project = ProjectToolRunner(
                    self.runner.executor, ctx.project_root,
                    network_allowed=lambda host: ctx.policy.network(host).allowed,
                    sandbox_network=docker.network == "bridge",
                    timeout_seconds=self.runner.timeout,
                )  # fmt: skip
                return await project.run(self.adapter)
            return await self.runner.run(self.adapter)
        except (ExecutorError, ExecutorUnavailableError) as exc:
            return self._failed(str(exc)[:400])

    def _failed(self, error: str) -> ToolRun:
        spec = self.adapter.spec
        return ToolRun(
            tool=spec.name, category=spec.category, version=None,
            backend=self.runner.executor.backend.value, exit_code=-1, succeeded=False,
            findings=(), duration_seconds=0.0, error=error,
        )  # fmt: skip


def _tool_runs(ctx: StepContext) -> list[ToolRun]:
    return [
        ToolRun.from_dict(dict(out))
        for key, out in sorted(ctx.dependencies.items())
        if key.startswith("scan.")
    ]


class GatesHandler(_Handler):
    """Evaluates the Quality Gates from the scan results and the baseline."""

    name = "builtin.quality.gates"
    capabilities = frozenset({"quality.gates"})

    def __init__(self, *, strict: bool) -> None:
        self.strict = strict

    async def run(self, ctx: StepContext) -> StepResult:
        try:
            baseline = load_baseline(ctx.project_root)
        except BaselineError as exc:
            raise PermanentError(str(exc)) from exc
        report = QualityGateEngine(ctx.config.quality_gates, strict=self.strict).evaluate(
            _tool_runs(ctx), baseline
        )
        output = report.to_dict()
        output["suppressed"] = [f.fingerprint for f in report.suppressed]
        output["expired"] = [f.fingerprint for f in report.expired_baseline]
        return StepResult(output=output)


class IntegrityHandler(_Handler):
    """Fails the run if the read-only audit changed the working tree (FR-06)."""

    name = "builtin.audit.integrity"
    capabilities = frozenset({"audit.integrity"})

    async def run(self, ctx: StepContext) -> StepResult:
        before = ctx.dependencies["audit.snapshot"]["digest"]
        after, files = await asyncio.to_thread(snapshot_digest, ctx.project_root)
        if after != before:
            raise PermanentError(
                "the working tree changed during a read-only audit (FR-06); "
                "results are not trustworthy"
            )
        return StepResult(
            output={"unchanged": True, "method": "path/size/mtime digest", "files": files}
        )


class AuditReportHandler(_Handler):
    """Builds the JSON and Markdown reports and stores them as artifacts."""

    name = "builtin.report.audit"
    capabilities = frozenset({"report.audit"})

    def __init__(self, *, backend: ExecutionBackend) -> None:
        self.backend = backend

    async def run(self, ctx: StepContext) -> StepResult:
        deps = ctx.dependencies
        gates = deps["quality.gates"]
        suppressed, expired = set(gates["suppressed"]), set(gates["expired"])
        runs = _tool_runs(ctx)
        findings: list[dict[str, Any]] = []
        for f in (finding for r in runs for finding in r.findings):
            item = f.to_dict()
            item["status"] = (
                "baselined" if f.fingerprint in suppressed
                else "expired-baseline" if f.fingerprint in expired
                else "new"
            )  # fmt: skip
            findings.append(item)
        report = AuditReport(
            project=ctx.config.project.name,
            run_id=str(ctx.run_id),
            generated_at=utcnow().isoformat(timespec="seconds"),
            backend=self.backend.value,
            verdict=gates["verdict"],
            gates=gates["gates"],
            tools=[r.to_dict() for r in runs],
            findings=findings,
            baseline=gates["baseline"],
            integrity=dict(deps["audit.integrity"]),
            discovery={k: deps[k] for k in sorted(deps) if k.startswith("discover.")},
            notes=list(gates["notes"]),
        )
        json_ref = await asyncio.to_thread(
            ctx.save_artifact, "audit-report.json", report.to_json().encode(), "application/json"
        )
        md_ref = await asyncio.to_thread(
            ctx.save_artifact, "audit-report.md", render_markdown(report).encode(), "text/markdown"
        )
        return StepResult(
            output={
                "verdict": report.verdict,
                "artifacts": {"json": json_ref.uri, "markdown": md_ref.uri},
                "summary": report.severity_counts(),
            },
            evidence_refs=[json_ref.uri, md_ref.uri],
        )


# -------------------------------------------------------------------- builder --
def make_executor(
    config: AICodingConfig, backend: ExecutionBackend, binaries: set[str]
) -> Executor:
    """Executor for ``backend`` configured from the project limits."""
    limits = config.execution.limits
    if backend is ExecutionBackend.LOCAL:
        return LocalExecutor(allowed_executables=binaries, limits=limits)
    if backend is ExecutionBackend.DOCKER:
        return DockerSandboxExecutor(limits=limits, docker_config=config.execution.docker)
    raise PermanentError("the github-actions backend arrives in S4; use local or docker")


def effective_stacks(config: AICodingConfig, root: Path) -> list[Stack]:
    """Configured stacks, or detected ones when the configuration lists none."""
    return list(config.stacks) or [e.stack for e in detect_stacks(root)]


def build_audit(
    config: AICodingConfig, root: Path, options: AuditOptions
) -> tuple[HandlerRegistry, list[StepSpec], list[AnyTool]]:
    """Handlers, DAG steps and selected tools of an audit run."""
    adapters = default_tool_registry().select(
        effective_stacks(config, root), only=list(options.tools) or None
    )
    executor = make_executor(
        config, options.backend,
        {a.spec.binary for a in adapters if not isinstance(a, ProjectTool)},
    )  # fmt: skip
    runner = ToolRunner(
        executor,
        root,
        timeout_seconds=float(config.execution.limits.timeout_seconds),
        semgrep_config=options.semgrep_config,
    )
    registry = HandlerRegistry(
        [
            ConfigDiscoveryHandler(),
            GitDiscoveryHandler(),
            StackDiscoveryHandler(),
            SnapshotHandler(),
            *(ToolScanHandler(a, runner) for a in adapters),
            GatesHandler(strict=options.strict),
            IntegrityHandler(),
            AuditReportHandler(backend=options.backend),
        ]
    )
    timeout = float(config.execution.limits.timeout_seconds)
    discovery_caps = {
        "discover.config": "discovery.config",
        "discover.git": "discovery.git",
        "discover.stacks": "discovery.stacks",
        "audit.snapshot": "audit.snapshot",
    }
    discovery = list(discovery_caps)
    steps = [
        StepSpec(key=k, capability=cap, phase=TaskPhase.DISCOVERY, timeout_seconds=300.0)
        for k, cap in discovery_caps.items()
    ]
    scans = [f"scan.{a.spec.name}" for a in adapters]
    steps += [
        StepSpec(key=k, capability=k, phase=TaskPhase.EXECUTION, timeout_seconds=timeout + 60,
                 depends_on=("discover.config", "audit.snapshot"))
        for k in scans
    ]  # fmt: skip
    steps += [
        StepSpec(key="quality.gates", capability="quality.gates",
                 phase=TaskPhase.VERIFICATION, depends_on=tuple(scans)),
        StepSpec(key="audit.integrity", capability="audit.integrity",
                 phase=TaskPhase.VERIFICATION, depends_on=("audit.snapshot", *scans)),
        StepSpec(key="report.audit", capability="report.audit", phase=TaskPhase.VERIFICATION,
                 depends_on=(*discovery, *scans, "quality.gates", "audit.integrity"),
                 success_criteria=_REPORT_CRITERIA),
    ]  # fmt: skip
    return registry, steps, adapters


_REPORT_CRITERIA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "artifacts"],
    "properties": {
        "verdict": {"enum": ["PASS", "FAIL"]},
        "artifacts": {"type": "object", "required": ["json", "markdown"]},
    },
}


def findings_of(runs: Sequence[ToolRun]) -> list[Finding]:
    """All findings of ``runs``."""
    return [f for r in runs for f in r.findings]
