"""Agent Router: picks a handler by capability, permission, cost and availability (ТЗ §5)."""

from __future__ import annotations

from dataclasses import dataclass

from ai_coding.orchestrator.errors import NoRouteError
from ai_coding.orchestrator.policy import PolicyEngine
from ai_coding.orchestrator.registry import HandlerRegistry, StepHandler


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Chosen handler and the candidates that were rejected (for the audit trail)."""

    handler: StepHandler
    rejected: tuple[tuple[str, str], ...]


class AgentRouter:
    """Deterministic routing: cheapest permitted, available handler; ties by name."""

    def __init__(self, registry: HandlerRegistry, policy: PolicyEngine) -> None:
        self._registry = registry
        self._policy = policy

    def route(
        self, capability: str, *, max_cost_usd: float | None = None, preferred: str | None = None
    ) -> RouteDecision:
        """Select a handler or raise :class:`NoRouteError` explaining why none fits."""
        candidates = self._registry.for_capability(capability)
        if preferred:
            candidates = [c for c in candidates if c.name == preferred]
        rejected: list[tuple[str, str]] = []
        eligible: list[StepHandler] = []
        for handler in candidates:
            decision = self._policy.agent(handler.name)
            if not decision.allowed:
                rejected.append((handler.name, decision.reason))
            elif max_cost_usd is not None and max_cost_usd > 0 and handler.cost_usd > max_cost_usd:
                rejected.append(
                    (handler.name, f"cost ${handler.cost_usd} > budget ${max_cost_usd}")
                )
            elif not handler.is_available():
                rejected.append((handler.name, "not available"))
            else:
                eligible.append(handler)
        if not eligible:
            detail = "; ".join(f"{n}: {r}" for n, r in rejected) or "no handler registered"
            raise NoRouteError(f"no handler for capability '{capability}' ({detail})")
        eligible.sort(key=lambda h: (h.cost_usd, h.name))
        return RouteDecision(handler=eligible[0], rejected=tuple(rejected))
