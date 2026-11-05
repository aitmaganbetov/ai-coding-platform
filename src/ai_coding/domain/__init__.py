"""Core domain vocabulary shared by every module (no I/O, no third-party deps)."""

from ai_coding.domain.enums import (
    AgentName,
    ApprovalOperation,
    ApprovalStatus,
    ExecutionBackend,
    GateResult,
    Mode,
    NetworkPolicy,
    Outcome,
    ProviderName,
    Role,
    RunState,
    Severity,
    Stack,
    TaskPhase,
    TaskStatus,
)

__all__ = [
    "AgentName",
    "ApprovalOperation",
    "ApprovalStatus",
    "ExecutionBackend",
    "GateResult",
    "Mode",
    "NetworkPolicy",
    "Outcome",
    "ProviderName",
    "Role",
    "RunState",
    "Severity",
    "Stack",
    "TaskPhase",
    "TaskStatus",
]
