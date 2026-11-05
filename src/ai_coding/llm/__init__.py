"""LLM provider port, guard and usage accounting (ADR-0009).

Concrete provider adapters (OpenAI, Anthropic, Ollama, vLLM) arrive in S5; this
package defines the contract every adapter implements and the guard that every
call goes through.
"""

from ai_coding.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
)
from ai_coding.llm.guard import (
    GuardedLLM,
    InvalidModelOutputError,
    LLMApprovalRequiredError,
    LLMBudgetExceededError,
    LLMPolicyDeniedError,
)

__all__ = [
    "GuardedLLM",
    "InvalidModelOutputError",
    "LLMApprovalRequiredError",
    "LLMBudgetExceededError",
    "LLMPolicyDeniedError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "TokenUsage",
]
