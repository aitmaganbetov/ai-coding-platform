"""Guard around every LLM call (ТЗ §12, FR-15, FR-18).

Order of checks for each call:

1. policy: provider must be allowed by egress policy; external providers may
   require approval (``external_llm``);
2. budget pre-check with an estimate of the request size;
3. redaction of every message (secrets never leave the process);
4. the provider call;
5. usage accounting (``model_usage``) and budget update;
6. structured-output validation against ``response_schema`` — invalid output is an
   error, never a success (ТЗ §6);
7. redaction of the response text before it is returned or logged.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ai_coding.domain.enums import ProviderName
from ai_coding.domain.errors import PermanentError
from ai_coding.llm.base import LLMProvider, LLMRequest, LLMResponse, TokenUsage
from ai_coding.security import Redactor, default_redactor


class LLMPolicyDeniedError(PermanentError):
    """Policy forbids sending this request to the provider."""


class LLMApprovalRequiredError(PermanentError):
    """Sending to the provider requires a human approval."""


class LLMBudgetExceededError(PermanentError):
    """The run budget does not allow this call."""


class InvalidModelOutputError(PermanentError):
    """The model output does not satisfy the requested schema."""


class GateDecision(Protocol):
    """Subset of a policy decision the guard needs."""

    @property
    def allowed(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    @property
    def verdict(self) -> str: ...


class PolicyGate(Protocol):
    """Anything that can authorise an LLM call (the orchestrator's PolicyEngine)."""

    def external_llm(
        self, provider: ProviderName, *, base_url: str | None = None
    ) -> GateDecision: ...


class Budget(Protocol):
    """Budget accounting (the orchestrator's BudgetTracker)."""

    def check(self, *, extra_tokens: int = 0, extra_cost_usd: float = 0.0) -> None: ...

    def add(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None: ...


UsageRecorder = Callable[[ProviderName, str, TokenUsage, float], None]


def _estimate_tokens(request: LLMRequest) -> int:
    chars = sum(len(m.content) for m in request.messages)
    return math.ceil(chars / 4) + request.max_output_tokens


class GuardedLLM:
    """Wraps a provider; the only way orchestrator code talks to an LLM."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        policy: PolicyGate,
        budget: Budget,
        record_usage: UsageRecorder | None = None,
        redactor: Redactor = default_redactor,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._budget = budget
        self._record = record_usage
        self._redactor = redactor

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run a guarded completion."""
        decision = self._policy.external_llm(self._provider.name, base_url=self._provider.base_url)
        if not decision.allowed:
            if str(decision.verdict) == "require_approval":
                raise LLMApprovalRequiredError(decision.reason)
            raise LLMPolicyDeniedError(decision.reason)
        try:
            self._budget.check(extra_tokens=_estimate_tokens(request))
        except PermanentError as exc:
            raise LLMBudgetExceededError(str(exc)) from exc

        safe = LLMRequest(
            model=request.model,
            messages=tuple(
                m.with_content(self._redactor.redact(m.content)) for m in request.messages
            ),
            max_output_tokens=request.max_output_tokens,
            temperature=request.temperature,
            response_schema=request.response_schema,
            metadata=request.metadata,
        )
        response = await self._provider.complete(safe)
        self._budget.add(tokens=response.usage.total, cost_usd=response.cost_usd)
        if self._record is not None:
            self._record(self._provider.name, response.model, response.usage, response.cost_usd)

        text = self._redactor.redact(response.text)
        parsed: dict[str, Any] | None = None
        if request.response_schema is not None:
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InvalidModelOutputError(f"model output is not JSON: {exc.msg}") from exc
            errors = sorted(
                Draft202012Validator(dict(request.response_schema)).iter_errors(candidate),
                key=lambda e: list(e.absolute_path),
            )
            if errors:
                first = errors[0]
                where = "/".join(str(p) for p in first.absolute_path) or "<root>"
                raise InvalidModelOutputError(
                    f"model output violates schema at {where}: {first.message}"
                )
            parsed = candidate
        return LLMResponse(
            text=text,
            model=response.model,
            usage=response.usage,
            cost_usd=response.cost_usd,
            parsed=parsed,
        )
