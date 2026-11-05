"""Provider-neutral request/response types and the ``LLMProvider`` port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

from ai_coding.domain.enums import ProviderName

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    """One chat message. ``untrusted`` marks repository/issue content (ТЗ §12)."""

    role: Role
    content: str
    untrusted: bool = False

    def with_content(self, content: str) -> Message:
        """Copy with different content."""
        return replace(self, content=content)


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Provider-neutral completion request."""

    model: str
    messages: tuple[Message, ...]
    max_output_tokens: int = 4096
    temperature: float = 0.0
    response_schema: Mapping[str, Any] | None = None  # JSON Schema for structured output
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting reported by the provider."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        """Input + output tokens."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Provider-neutral completion response."""

    text: str
    model: str
    usage: TokenUsage = TokenUsage()
    cost_usd: float = 0.0
    parsed: Mapping[str, Any] | None = None  # set by the guard after schema validation
    cached: bool = False  # served from the response cache


@runtime_checkable
class LLMProvider(Protocol):
    """Port implemented by every provider adapter."""

    name: ProviderName
    base_url: str | None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion."""
        ...
