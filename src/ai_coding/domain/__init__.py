"""Core domain vocabulary shared by every module (no I/O, no third-party deps)."""

from ai_coding.domain.enums import (
    AgentName,
    ApprovalOperation,
    ExecutionBackend,
    GateResult,
    Mode,
    NetworkPolicy,
    ProviderName,
    Role,
    RunState,
    Severity,
    Stack,
)

__all__ = [
    "AgentName",
    "ApprovalOperation",
    "ExecutionBackend",
    "GateResult",
    "Mode",
    "NetworkPolicy",
    "ProviderName",
    "Role",
    "RunState",
    "Severity",
    "Stack",
]
