"""LLM port, provider adapters, guard, routing and response cache (ADR-0009, ADR-0023).

Every call goes through :class:`GuardedLLM` (policy, budget, redaction, schema
validation); :class:`RoutedLLM` chains guarded calls across the configured primary
model and its fallbacks.
"""

from ai_coding.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
)
from ai_coding.llm.cache import ResponseCache
from ai_coding.llm.guard import (
    GuardedLLM,
    InvalidModelOutputError,
    LLMApprovalRequiredError,
    LLMBudgetExceededError,
    LLMPolicyDeniedError,
)
from ai_coding.llm.providers import (
    AnthropicProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderRequestError,
    ProviderUnavailableError,
    TruncatedOutputError,
    VLLMProvider,
    make_provider,
)
from ai_coding.llm.router import (
    AllRoutesFailedError,
    AllRoutesUnavailableError,
    Attempt,
    Route,
    RoutedLLM,
)

__all__ = [
    "AllRoutesFailedError",
    "AllRoutesUnavailableError",
    "AnthropicProvider",
    "Attempt",
    "GuardedLLM",
    "InvalidModelOutputError",
    "LLMApprovalRequiredError",
    "LLMBudgetExceededError",
    "LLMPolicyDeniedError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderRequestError",
    "ProviderUnavailableError",
    "ResponseCache",
    "Route",
    "RoutedLLM",
    "TokenUsage",
    "TruncatedOutputError",
    "VLLMProvider",
    "make_provider",
]
