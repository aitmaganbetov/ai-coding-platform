"""Agent contracts (ТЗ §6; ADR-0011)."""

from ai_coding.contracts.agent import (
    AgentResult,
    AgentResultStatus,
    AgentScope,
    AgentTask,
    OutputValidation,
    validate_agent_output,
)

__all__ = [
    "AgentResult",
    "AgentResultStatus",
    "AgentScope",
    "AgentTask",
    "OutputValidation",
    "validate_agent_output",
]
