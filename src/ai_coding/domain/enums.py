"""Enumerations defined by the technical specification (ТЗ v2.1).

Values are lower-case strings because they appear verbatim in ``.ai-coding.yml``,
the REST API and persisted records; changing a value is a breaking change and
requires an ADR.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Lifecycle mode of a run (ТЗ §2)."""

    CREATE = "create"
    DEVELOP = "develop"
    MODERNIZE = "modernize"
    AUDIT = "audit"
    MAINTAIN = "maintain"


class RunState(StrEnum):
    """Run/task states (ТЗ §5). The transition table lives in the orchestrator (S2)."""

    CREATED = "CREATED"
    DISCOVERY = "DISCOVERY"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class Role(StrEnum):
    """RBAC roles (ТЗ §3)."""

    OWNER = "owner"
    MAINTAINER = "maintainer"
    DEVELOPER = "developer"
    AUDITOR = "auditor"
    AGENT = "agent"


class Stack(StrEnum):
    """Technology stacks the platform recognises (FR-02)."""

    LARAVEL = "laravel"
    PHP = "php"
    REACT = "react"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    GO = "go"
    RUST = "rust"


class ExecutionBackend(StrEnum):
    """Executor implementations (ТЗ §4, §15)."""

    LOCAL = "local"
    DOCKER = "docker"
    GITHUB_ACTIONS = "github-actions"


class ProviderName(StrEnum):
    """LLM provider adapters (ТЗ §4). ``none`` disables AI entirely (FR-12)."""

    NONE = "none"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    VLLM = "vllm"

    @property
    def is_local(self) -> bool:
        """Whether the provider can run without egress to the public internet."""
        return self in {ProviderName.NONE, ProviderName.OLLAMA, ProviderName.VLLM}


class NetworkPolicy(StrEnum):
    """Egress policy (ТЗ §12). ``local-only`` forbids any egress."""

    LOCAL_ONLY = "local-only"
    ALLOWLIST = "allowlist"
    OPEN = "open"


class AgentName(StrEnum):
    """The twelve agents defined in ТЗ §6."""

    REQUIREMENTS = "requirements"
    ARCHITECT = "architect"
    PLANNER = "planner"
    BOOTSTRAP = "bootstrap"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    SECURITY = "security"
    QA = "qa"
    DATABASE = "database"
    PERFORMANCE = "performance"
    DEVOPS = "devops"
    MAINTENANCE = "maintenance"


class ApprovalOperation(StrEnum):
    """Operations that may require human approval (ТЗ §5 Human-in-the-loop, FR-17)."""

    CRITICAL_FILE_CHANGE = "critical_file_change"
    DELETE = "delete"
    MIGRATION = "migration"
    MERGE = "merge"
    DEPLOY = "deploy"
    PR_CREATE = "pr_create"
    DEPENDENCY_UPDATE = "dependency_update"
    EXTERNAL_LLM = "external_llm"
    HOST_COMMAND = "host_command"
    SPEC_APPROVAL = "spec_approval"  # CREATE: the requirements version must be approved (FR-03)

    @classmethod
    def mandatory(cls) -> frozenset[ApprovalOperation]:
        """Operations whose approval can never be disabled by configuration (ТЗ §12, FR-17)."""
        return frozenset({cls.DELETE, cls.MIGRATION, cls.MERGE, cls.DEPLOY})


class Severity(StrEnum):
    """Finding severity, ordered from most to least severe."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Numeric rank: 0 is the most severe."""
        return list(Severity).index(self)


class GateResult(StrEnum):
    """Outcome of a deterministic Quality Gate (ТЗ §10)."""

    PASS = "PASS"  # noqa: S105 - not a password
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class TaskStatus(StrEnum):
    """Status of a single DAG step inside a run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """No further transitions are possible."""
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.SKIPPED,
            TaskStatus.CANCELLED,
        }


class TaskPhase(StrEnum):
    """Run phase a task belongs to; maps onto the run FSM (ТЗ §5)."""

    DISCOVERY = "discovery"
    EXECUTION = "execution"
    VERIFICATION = "verification"


class ApprovalStatus(StrEnum):
    """Lifecycle of a human approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Outcome(StrEnum):
    """Outcome recorded on every audit event (ТЗ §3)."""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    NOOP = "noop"
