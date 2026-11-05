"""Run kinds the server can execute, with their parameters and required permission.

The kind and its validated parameters are stored in the run's options, so any worker
can rebuild the same handlers to execute or resume the run (ADR-0013, ADR-0030).
Audits on the server always use the Docker sandbox: analysers never run on the host.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_coding.api.rbac import Permission
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import ExecutionBackend, Mode
from ai_coding.orchestrator import HandlerRegistry, StepSpec
from ai_coding.orchestrator.handlers import builtin_registry, discovery_plan
from ai_coding.workflows import AuditOptions, build_audit
from ai_coding.workflows.develop import DevelopRequest, build_develop
from ai_coding.workflows.implement import ImplementSettings
from ai_coding.workflows.maintain import MaintainRequest, build_maintain

Built = tuple[HandlerRegistry, list[StepSpec]]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoverParams(_Params):
    """No parameters."""


class AuditParams(_Params):
    tools: list[str] = Field(default_factory=list, max_length=50)
    strict: bool = False
    triage: bool = False


class DevelopParams(_Params):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20_000)
    base: str | None = Field(default=None, max_length=255)
    max_iterations: int = Field(default=3, ge=1, le=10)


class MaintainParams(_Params):
    issues: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    dependencies: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    telemetry: str | None = Field(default=None, max_length=20_000)
    base: str | None = Field(default=None, max_length=255)


@dataclass(frozen=True, slots=True)
class RunKind:
    """A kind of run the API accepts."""

    name: str
    mode: Mode
    permission: Permission
    params: type[_Params]
    build: Callable[[Path, AICodingConfig, Mapping[str, Any]], tuple[Built, dict[str, Any]]]
    description: str


def _discover(root: Path, config: AICodingConfig, p: Mapping[str, Any]) -> tuple[Built, dict]:  # type: ignore[type-arg]
    steps = discovery_plan(max_attempts=config.execution.limits.max_retries + 1)
    return (builtin_registry(), steps), dict(p)


def _audit(root: Path, config: AICodingConfig, p: Mapping[str, Any]) -> tuple[Built, dict]:  # type: ignore[type-arg]
    params = AuditParams.model_validate(p)
    options = AuditOptions(
        backend=ExecutionBackend.DOCKER,
        tools=tuple(params.tools),
        strict=params.strict,
        triage=params.triage,
    )
    registry, steps, adapters = build_audit(config, root, options)
    # Pin the selected tools: a resumed run must get exactly the same handlers.
    fixed = {**params.model_dump(), "tools": [a.spec.name for a in adapters]}
    return (registry, steps), fixed


def _develop(root: Path, config: AICodingConfig, p: Mapping[str, Any]) -> tuple[Built, dict]:  # type: ignore[type-arg]
    params = DevelopParams.model_validate(p)
    request = DevelopRequest(
        title=params.title,
        body=params.body,
        base=params.base or config.project.default_branch,
        settings=ImplementSettings(max_iterations=params.max_iterations),
    )
    fixed = {**params.model_dump(), "base": request.base}
    return build_develop(request), fixed


def _maintain(root: Path, config: AICodingConfig, p: Mapping[str, Any]) -> tuple[Built, dict]:  # type: ignore[type-arg]
    params = MaintainParams.model_validate(p)
    request = MaintainRequest(
        issues=tuple(params.issues),
        findings=tuple(params.findings),
        dependencies=tuple(params.dependencies),
        telemetry=params.telemetry,
        base=params.base or config.project.default_branch,
    )
    return build_maintain(request), {**params.model_dump(), "base": request.base}


KINDS: dict[str, RunKind] = {
    k.name: k
    for k in (
        RunKind(
            "discover",
            Mode.AUDIT,
            Permission.RUN_CREATE,
            DiscoverParams,
            _discover,
            "Discovery: configuration, git state, stacks, context index (offline)",
        ),
        RunKind(
            "audit",
            Mode.AUDIT,
            Permission.RUN_CREATE,
            AuditParams,
            _audit,
            "Read-only audit in the Docker sandbox → Quality Gates → report",
        ),
        RunKind(
            "develop",
            Mode.DEVELOP,
            Permission.RUN_CREATE_CHANGES,
            DevelopParams,
            _develop,
            "Implement a task on a work branch with sandboxed tests and review",
        ),
        RunKind(
            "maintain",
            Mode.MAINTAIN,
            Permission.RUN_CREATE_CHANGES,
            MaintainParams,
            _maintain,
            "Maintenance plan → approval → one verified fix",
        ),
    )
}


def build_kind(
    name: str, root: Path, config: AICodingConfig, params: Mapping[str, Any]
) -> tuple[Built, dict[str, Any]]:
    """Handlers, steps and the normalised parameters of a run of kind ``name``."""
    kind = KINDS.get(name)
    if kind is None:
        raise KeyError(f"unknown run kind '{name}' (known: {', '.join(sorted(KINDS))})")
    kind.params.model_validate(params)
    return kind.build(root, config, params)
