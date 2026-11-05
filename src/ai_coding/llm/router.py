"""Model routing and fallback chains (FR-15; ADR-0009, ADR-0023).

A :class:`RoutedLLM` holds an ordered chain of routes (primary first, then the
configured fallbacks). Each attempt goes through its own :class:`GuardedLLM`, so the
egress policy, redaction, budget and schema validation apply to every provider in the
chain, not only to the first one.

What moves the call to the next route:

* the provider is unavailable (network, timeout, rate limit, 5xx);
* the provider rejected the request (auth, unknown model, refusal, truncation);
* the answer is not valid against the output schema;
* the policy denies this provider (e.g. an external provider under ``local-only``).

What stops the chain immediately, because no other provider can fix it:

* a required human approval (``external_llm``) — the run must pause;
* the run budget is exhausted.

When every route fails, :class:`AllRoutesFailedError` is raised; it is transient only
if every failure was transient, so the orchestrator retries only when that can help.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from ai_coding.domain.errors import PermanentError, TransientError
from ai_coding.llm.base import LLMProvider, LLMRequest, LLMResponse
from ai_coding.llm.cache import ResponseCache
from ai_coding.llm.guard import (
    Budget,
    GuardedLLM,
    LLMApprovalRequiredError,
    LLMBudgetExceededError,
    PolicyGate,
    UsageRecorder,
)
from ai_coding.security import Redactor, default_redactor, redact


@dataclass(frozen=True, slots=True)
class Route:
    """One provider + model in a chain."""

    provider: LLMProvider
    model: str

    @property
    def label(self) -> str:
        """``provider/model`` for logs and errors."""
        return f"{self.provider.name.value}/{self.model}"


@dataclass(frozen=True, slots=True)
class Attempt:
    """Outcome of one route in a chain (for logs and error messages)."""

    route: str
    error: str
    transient: bool


class AllRoutesFailedError(PermanentError):
    """Every route of the chain failed; ``attempts`` says why."""

    def __init__(self, attempts: Sequence[Attempt]) -> None:
        detail = "; ".join(f"{a.route}: {a.error}" for a in attempts) or "no routes"
        super().__init__(f"all model routes failed: {detail}")
        self.attempts = tuple(attempts)


class AllRoutesUnavailableError(TransientError):
    """Every route failed transiently; retrying later may help."""

    def __init__(self, attempts: Sequence[Attempt]) -> None:
        detail = "; ".join(f"{a.route}: {a.error}" for a in attempts)
        super().__init__(f"all model routes unavailable: {detail}")
        self.attempts = tuple(attempts)


@dataclass
class RoutedLLM:
    """Chain of guarded routes; the object agents call."""

    routes: Sequence[Route]
    policy: PolicyGate
    budget: Budget
    record_usage: UsageRecorder | None = None
    cache: ResponseCache | None = None
    redactor: Redactor = field(default_factory=lambda: default_redactor)
    on_attempt: Callable[[Attempt], None] | None = None
    attempts: list[Attempt] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("a routed LLM needs at least one route")

    @property
    def primary(self) -> Route:
        """First route of the chain."""
        return self.routes[0]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Try each route in order; ``request.model`` is replaced by the route's model."""
        failures: list[Attempt] = []
        for route in self.routes:
            guarded = GuardedLLM(
                route.provider,
                policy=self.policy,
                budget=self.budget,
                record_usage=self.record_usage,
                redactor=self.redactor,
                cache=self.cache,
            )
            try:
                return await guarded.complete(replace(request, model=route.model))
            except (LLMApprovalRequiredError, LLMBudgetExceededError):
                raise
            except (TransientError, PermanentError) as exc:
                attempt = Attempt(
                    route=route.label,
                    error=redact(str(exc))[:300],
                    transient=isinstance(exc, TransientError),
                )
                failures.append(attempt)
                self.attempts.append(attempt)
                if self.on_attempt is not None:
                    self.on_attempt(attempt)
        if failures and all(a.transient for a in failures):
            raise AllRoutesUnavailableError(failures)
        raise AllRoutesFailedError(failures)
