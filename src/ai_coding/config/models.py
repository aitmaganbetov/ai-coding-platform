"""Typed model of ``.ai-coding.yml`` (ТЗ §11).

The Pydantic models are the single source of truth; the JSON Schema shipped in
``ai_coding/schemas/ai-coding.schema.json`` is generated from them
(``ai-coding config schema``) and a test guarantees the two never drift (ADR-0004).

Defaults are *secure by default*: AI disabled, local executor, local-only network,
all Quality Gates blocking, mandatory approvals enabled.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_coding.domain.enums import (
    AgentName,
    ApprovalOperation,
    ExecutionBackend,
    Mode,
    NetworkPolicy,
    ProviderName,
    Severity,
    Stack,
)
from ai_coding.security.endpoints import is_local_endpoint

SCHEMA_VERSION = "2.1"
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({SCHEMA_VERSION})

_ENV_NAME = r"^[A-Z_][A-Z0-9_]*$"
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = rf"^(\*\.)?{_LABEL}(\.{_LABEL})*(:\d{{1,5}})?$"
_REL_PATH = r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$"  # project-relative; ".." refused below
_BRANCH_GLOB = r"^[A-Za-z0-9._/*-]+$"

ReportFormat = Literal["json", "markdown", "sarif", "junit"]


def _default_formats() -> list[ReportFormat]:
    return ["json", "markdown"]


EnvVarName = Annotated[str, Field(pattern=_ENV_NAME, max_length=128)]


class _Strict(BaseModel):
    """Base: unknown keys are errors so that typos never silently disable a policy."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        validate_default=True,
    )


# ------------------------------------------------------------------ project --
class ProjectConfig(_Strict):
    """Project identity."""

    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Machine-friendly project name (letters, digits, '.', '_', '-').",
    )
    description: str | None = Field(default=None, max_length=2000)
    default_branch: str = Field(default="main", pattern=_BRANCH_GLOB, max_length=255)
    protected_branches: list[Annotated[str, Field(pattern=_BRANCH_GLOB)]] = Field(
        default_factory=lambda: ["main", "master", "release/*"],
        description="Branches the platform never pushes or commits to directly (AC-03).",
    )

    @model_validator(mode="after")
    def _default_branch_is_protected(self) -> Self:
        if not any(_glob_match(p, self.default_branch) for p in self.protected_branches):
            raise ValueError(
                f"default_branch '{self.default_branch}' must be covered by protected_branches"
            )
        return self


# ---------------------------------------------------------------- execution --
class ResourceLimits(_Strict):
    """Per-step resource limits enforced by every executor (ТЗ §5 Executor)."""

    cpus: float = Field(default=2.0, gt=0, le=64)
    memory_mb: int = Field(default=2048, ge=128, le=262144)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_parallel_tasks: int = Field(default=4, ge=1, le=256)


class DockerConfig(_Strict):
    """Docker sandbox settings (ТЗ §12: no privileged, no Docker socket)."""

    image: str = Field(default="python:3.12-slim", min_length=1, max_length=255)
    read_only_root: bool = True
    network: Literal["none", "bridge"] = "none"
    user: str = Field(default="1000:1000", pattern=r"^\d+(:\d+)?$")
    runtime: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9._-]{0,63}$",
        description="OCI runtime for the sandbox containers, e.g. 'runsc' (gVisor: a "
        "user-space kernel between the project code and the host kernel); the daemon must "
        "have it registered. Default: the daemon's default runtime (runc).",
    )


class ExecutionConfig(_Strict):
    """Where and how commands run."""

    backend: ExecutionBackend = ExecutionBackend.LOCAL
    use_worktree: bool = Field(
        default=True, description="Apply code changes in an isolated git worktree."
    )
    allow_host_commands: bool = Field(
        default=False,
        description="Allow the local executor to run allow-listed commands on the host.",
    )
    command_allowlist: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list,
        description="Executables the local executor may invoke without approval.",
    )
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    docker: DockerConfig = Field(default_factory=DockerConfig)


# ----------------------------------------------------------------------- ai --
class ModelRoute(_Strict):
    """Per-agent model override or fallback entry (FR-15 routing).

    ``base_url``/``api_key_env`` default to the top-level ``ai`` values when the
    provider is the same, so a route only needs them for a different endpoint.
    """

    provider: ProviderName
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2048)
    api_key_env: EnvVarName | None = None


class ModelPrice(_Strict):
    """Price of a model in USD per million tokens (used for cost budgets)."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class AIConfig(_Strict):
    """LLM provider settings. AI is optional: deterministic checks never need it."""

    enabled: bool = False
    provider: ProviderName = ProviderName.NONE
    model: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2048)
    api_key_env: EnvVarName | None = Field(
        default=None, description="Name of the environment variable holding the API key."
    )
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    fallback: list[ModelRoute] = Field(default_factory=list, max_length=5)
    routing: dict[AgentName, ModelRoute] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=5, le=3600)
    cache: bool = Field(
        default=True,
        description="Reuse responses for identical redacted requests at temperature 0.",
    )
    pricing: dict[Annotated[str, Field(min_length=1, max_length=200)], ModelPrice] = Field(
        default_factory=dict,
        description="USD per million tokens by model name; unpriced external models are "
        "limited by the token budget only.",
    )

    @model_validator(mode="after")
    def _provider_consistency(self) -> Self:
        if self.enabled and self.provider is ProviderName.NONE:
            raise ValueError("ai.enabled is true but ai.provider is 'none'")
        if self.enabled and not self.model:
            raise ValueError("ai.model is required when ai.enabled is true")
        if (
            self.enabled
            and self.provider in {ProviderName.OPENAI, ProviderName.ANTHROPIC}
            and not self.api_key_env
        ):
            raise ValueError(f"ai.api_key_env is required for provider '{self.provider}'")
        if self.enabled and self.provider is ProviderName.VLLM and not self.base_url:
            raise ValueError("ai.base_url is required for provider 'vllm'")
        if self.enabled:
            for where, route in [
                *(("ai.fallback", r) for r in self.fallback),
                *((f"ai.routing.{a.value}", r) for a, r in self.routing.items()),
            ]:
                self._check_route(where, route)
        return self

    def _check_route(self, where: str, route: ModelRoute) -> None:
        same = route.provider is self.provider
        if route.provider is ProviderName.NONE:
            raise ValueError(f"{where}: provider 'none' cannot serve requests")
        if (
            route.provider in {ProviderName.OPENAI, ProviderName.ANTHROPIC}
            and not route.api_key_env
            and not (same and self.api_key_env)
        ):
            raise ValueError(f"{where}: api_key_env is required for provider '{route.provider}'")
        if (
            route.provider is ProviderName.VLLM
            and not route.base_url
            and not (same and self.base_url)
        ):
            raise ValueError(f"{where}: base_url is required for provider 'vllm'")

    def resolve(self, route: ModelRoute | None = None) -> ModelRoute:
        """Fully specified route: the default model or ``route`` with inherited settings."""
        if route is None:
            return ModelRoute(
                provider=self.provider,
                model=self.model or "",
                base_url=self.base_url,
                api_key_env=self.api_key_env,
            )
        same = route.provider is self.provider
        return ModelRoute(
            provider=route.provider,
            model=route.model,
            base_url=route.base_url or (self.base_url if same else None),
            api_key_env=route.api_key_env or (self.api_key_env if same else None),
        )

    def routes_in_use(self) -> list[tuple[str, ModelRoute]]:
        """Every fully specified route that may receive requests, with its location."""
        if not self.enabled:
            return []
        return [
            ("ai", self.resolve()),
            *((f"ai.fallback[{i}]", self.resolve(r)) for i, r in enumerate(self.fallback)),
            *((f"ai.routing.{a.value}", self.resolve(r)) for a, r in self.routing.items()),
        ]

    def providers_in_use(self) -> set[ProviderName]:
        """All providers that may receive requests."""
        if not self.enabled:
            return set()
        return (
            {self.provider}
            | {r.provider for r in self.fallback}
            | {r.provider for r in self.routing.values()}
        )


# ------------------------------------------------------------------- agents --
class AgentsConfig(_Strict):
    """Which agents may be scheduled by the orchestrator."""

    enabled: list[AgentName] = Field(default_factory=lambda: list(AgentName))

    @field_validator("enabled")
    @classmethod
    def _unique(cls, value: list[AgentName]) -> list[AgentName]:
        if len(set(value)) != len(value):
            raise ValueError("agents.enabled contains duplicates")
        return value


# ----------------------------------------------------------------- security --
class SecurityConfig(_Strict):
    """Egress and data-protection policy (ТЗ §12, FR-18)."""

    network: NetworkPolicy = NetworkPolicy.LOCAL_ONLY
    egress_allowlist: list[Annotated[str, Field(pattern=_HOSTNAME, max_length=260)]] = Field(
        default_factory=list
    )
    redact_secrets: Literal[True] = Field(
        default=True, description="Secret redaction cannot be disabled."
    )
    treat_repository_content_as_untrusted: Literal[True] = True
    secret_scan: bool = True

    @model_validator(mode="after")
    def _allowlist_consistency(self) -> Self:
        if self.network is NetworkPolicy.ALLOWLIST and not self.egress_allowlist:
            raise ValueError("security.egress_allowlist must not be empty for network 'allowlist'")
        if self.network is NetworkPolicy.LOCAL_ONLY and self.egress_allowlist:
            raise ValueError("security.egress_allowlist must be empty for network 'local-only'")
        return self


# ------------------------------------------------------------ quality gates --
class _Gate(_Strict):
    enabled: bool = True
    blocking: bool = True


class BuildGate(_Gate):
    """Fails on build or dependency-installation errors."""


class TestsGate(_Gate):
    """Fails when required tests fail.

    Tests run in the Docker sandbox. Without ``command`` the platform picks a default
    for the detected stack (Python: pytest, Node.js: ``npm test`` / ``node --test``).
    ``install`` is the only step that may use the network, and only when
    ``execution.docker.network`` is ``bridge`` and the hosts are allowed by
    ``security`` (egress policy); otherwise tests that need it are SKIPPED.
    """

    command: str | None = Field(default=None, min_length=1, max_length=500)
    install: str | None = Field(default=None, min_length=1, max_length=500)
    install_hosts: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        default_factory=list, description="Hosts the install step needs (egress policy)."
    )
    image: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$",
        description="Digest-pinned runtime image (default: per stack).",
    )
    timeout_seconds: int = Field(default=900, ge=10, le=7200)
    e2e: bool = Field(
        default=True,
        description="Run the project's Playwright end-to-end tests in the sandbox (S9).",
    )
    performance_scripts: list[Annotated[str, Field(pattern=_REL_PATH, max_length=300)]] = Field(
        default_factory=list,
        description="k6 scripts, relative paths; empty: *.k6.js files and k6/*.js (S9).",
    )
    performance_hosts: list[Annotated[str, Field(pattern=_HOSTNAME, max_length=260)]] = Field(
        default_factory=list,
        description="Hosts of the system under test the k6 scripts load (egress allowlist; "
        "they must also be allowed by `security`); empty: k6 is SKIPPED.",
    )

    @field_validator("performance_scripts")
    @classmethod
    def _inside_project(cls, value: list[str]) -> list[str]:
        for path in value:
            if ".." in path.split("/"):
                raise ValueError(f"performance script {path!r} must stay inside the project")
        return value


class StaticGate(_Gate):
    """Fails when linter/type errors exceed the threshold."""

    max_errors: int = Field(default=0, ge=0)


class SecurityGate(_Gate):
    """Fails on confirmed findings at or above ``fail_on`` and on new secrets."""

    fail_on: Severity = Severity.HIGH
    block_new_secrets: bool = True


class CoverageGate(_Gate):
    """Fails when coverage of critical modules drops below ``min_percent``."""

    min_percent: float = Field(default=80.0, ge=0, le=100)


class ReviewGate(_Gate):
    """Fails without the required number of human approvals."""

    required_approvals: int = Field(default=1, ge=1, le=10)
    ai_review_blocking: bool = Field(
        default=False, description="AI review is advisory unless an approved policy says so."
    )


class QualityGatesConfig(_Strict):
    """Deterministic, AI-independent Quality Gates (ТЗ §10, FR-11)."""

    build: BuildGate = Field(default_factory=BuildGate)
    tests: TestsGate = Field(default_factory=TestsGate)
    static: StaticGate = Field(default_factory=StaticGate)
    security: SecurityGate = Field(default_factory=SecurityGate)
    coverage: CoverageGate = Field(default_factory=CoverageGate)
    review: ReviewGate = Field(default_factory=ReviewGate)


# ------------------------------------------------------------------ budgets --
class BudgetsConfig(_Strict):
    """Token/cost/time budgets per run (FR-15)."""

    max_tokens_per_run: int = Field(default=500_000, ge=0)
    max_cost_usd_per_run: float = Field(default=5.0, ge=0)
    max_duration_minutes: int = Field(default=120, ge=1, le=10080)


# ---------------------------------------------------------------- approvals --
class ApprovalsConfig(_Strict):
    """Human-in-the-loop policy (ТЗ §5, FR-17)."""

    required_for: list[ApprovalOperation] = Field(
        default_factory=lambda: sorted(
            ApprovalOperation.mandatory()
            | {
                ApprovalOperation.CRITICAL_FILE_CHANGE,
                ApprovalOperation.PR_CREATE,
                ApprovalOperation.EXTERNAL_LLM,
                ApprovalOperation.HOST_COMMAND,
            }
        )
    )
    critical_paths: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=lambda: [
            ".github/**",
            ".ai-coding.yml",
            "**/migrations/**",
            "Dockerfile",
            "**/*.lock",
        ]
    )
    auto_merge: Literal[False] = Field(default=False, description="Auto-merge is forbidden.")

    @field_validator("required_for")
    @classmethod
    def _mandatory_present(cls, value: list[ApprovalOperation]) -> list[ApprovalOperation]:
        missing = ApprovalOperation.mandatory() - set(value)
        if missing:
            names = ", ".join(sorted(m.value for m in missing))
            raise ValueError(f"approvals.required_for must include mandatory operations: {names}")
        if len(set(value)) != len(value):
            raise ValueError("approvals.required_for contains duplicates")
        return value


# ---------------------------------------------------------------- artifacts --
class ArtifactsConfig(_Strict):
    """Artifact storage and retention (NFR-08)."""

    store: Literal["local", "minio"] = "local"
    path: str = Field(default=".ai-coding/artifacts", min_length=1, max_length=1024)
    retention_days: int = Field(default=30, ge=1, le=3650)
    formats: list[ReportFormat] = Field(default_factory=_default_formats)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
            return value  # absolute paths are allowed (self-hosted volumes)
        if ".." in re.split(r"[\\/]", value):
            raise ValueError("artifacts.path must not escape the project with '..'")
        return value


# --------------------------------------------------------------------- root --
class AICodingConfig(_Strict):
    """Root of ``.ai-coding.yml``."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        title="AI Coding Platform project configuration (.ai-coding.yml)",
        json_schema_extra={
            "$id": "https://github.com/aitmaganbetov/ai-coding-platform/schemas/ai-coding.schema.json",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        },
    )

    schema_version: Literal["2.1"] = Field(description="Configuration schema version.")
    project: ProjectConfig
    mode: Mode = Mode.AUDIT
    stacks: list[Stack] = Field(default_factory=list)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    quality_gates: QualityGatesConfig = Field(default_factory=QualityGatesConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    approvals: ApprovalsConfig = Field(default_factory=ApprovalsConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)

    @field_validator("stacks")
    @classmethod
    def _unique_stacks(cls, value: list[Stack]) -> list[Stack]:
        if len(set(value)) != len(value):
            raise ValueError("stacks contains duplicates")
        return value

    @model_validator(mode="after")
    def _cross_section_rules(self) -> Self:
        external = {p for p in self.ai.providers_in_use() if not p.is_local}
        if external and self.security.network is NetworkPolicy.LOCAL_ONLY:
            names = ", ".join(sorted(p.value for p in external))
            raise ValueError(
                f"security.network 'local-only' forbids egress, but external AI provider(s) "
                f"are configured: {names}"
            )
        if self.security.network is NetworkPolicy.LOCAL_ONLY:
            # A local provider type is not enough: base_url decides where the code goes.
            for where, route in self.ai.routes_in_use():
                if route.base_url and not is_local_endpoint(route.base_url):
                    raise ValueError(
                        f"security.network 'local-only' forbids egress, but {where} sends "
                        f"requests to {route.base_url} (not a local or private host)"
                    )
        if self.execution.backend is ExecutionBackend.GITHUB_ACTIONS and (
            self.security.network is NetworkPolicy.LOCAL_ONLY
        ):
            raise ValueError(
                "execution.backend 'github-actions' requires network access; "
                "security.network 'local-only' forbids it"
            )
        if self.mode is Mode.AUDIT and self.execution.allow_host_commands:
            # AUDIT is read-only by default (FR-06); host execution must go through a sandbox.
            raise ValueError(
                "mode 'audit' is read-only: execution.allow_host_commands must be false"
            )
        return self


def _glob_match(pattern: str, value: str) -> bool:
    regex = "^" + re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
    return re.match(regex, value) is not None
