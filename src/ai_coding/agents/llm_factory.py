"""Build the routed LLM an agent uses from the project configuration (FR-15).

Chain = the agent's route (``ai.routing[agent]``) or the default model, followed by
``ai.fallback`` (duplicates removed). API keys are read from the environment by the
*name* given in the configuration (``api_key_env``); a route whose key is missing is
skipped and reported, never called without credentials.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ai_coding.config.models import AICodingConfig, ModelRoute
from ai_coding.domain.enums import AgentName
from ai_coding.llm.cache import ResponseCache
from ai_coding.llm.guard import Budget, PolicyGate, UsageRecorder
from ai_coding.llm.providers import make_provider
from ai_coding.llm.router import Route, RoutedLLM


class LLMUnavailableError(RuntimeError):
    """No route of the chain can be used (AI disabled or credentials missing)."""


@dataclass(frozen=True, slots=True)
class ChainPlan:
    """Resolved chain for an agent, before any provider is contacted."""

    routes: tuple[ModelRoute, ...]
    skipped: tuple[str, ...] = field(default_factory=tuple)


def plan_chain(
    config: AICodingConfig, agent: AgentName, env: Mapping[str, str] | None = None
) -> ChainPlan:
    """Routes for ``agent`` with credentials available in ``env``."""
    ai = config.ai
    if not ai.enabled:
        return ChainPlan(routes=(), skipped=("AI is disabled (ai.enabled: false)",))
    env = os.environ if env is None else env
    candidates = [ai.resolve(ai.routing.get(agent))] + [ai.resolve(r) for r in ai.fallback]
    seen: set[tuple[str, str, str | None]] = set()
    routes: list[ModelRoute] = []
    skipped: list[str] = []
    for route in candidates:
        key = (route.provider.value, route.model, route.base_url)
        if key in seen:
            continue
        seen.add(key)
        if route.api_key_env and not env.get(route.api_key_env):
            skipped.append(f"{route.provider.value}/{route.model}: ${route.api_key_env} is not set")
            continue
        routes.append(route)
    return ChainPlan(routes=tuple(routes), skipped=tuple(skipped))


def build_llm(
    config: AICodingConfig,
    agent: AgentName,
    *,
    policy: PolicyGate,
    budget: Budget,
    record_usage: UsageRecorder | None = None,
    cache_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RoutedLLM:
    """Routed, guarded LLM for ``agent``; raises :class:`LLMUnavailableError`."""
    env = os.environ if env is None else env
    plan = plan_chain(config, agent, env)
    if not plan.routes:
        raise LLMUnavailableError("no usable model route: " + "; ".join(plan.skipped))
    ai = config.ai
    pricing = {m: (p.input_per_mtok, p.output_per_mtok) for m, p in ai.pricing.items()}
    routes = [
        Route(
            make_provider(
                r.provider,
                base_url=r.base_url,
                api_key=env.get(r.api_key_env) if r.api_key_env else None,
                timeout=float(ai.timeout_seconds),
                pricing=pricing,
                transport=transport,
            ),
            r.model,
        )
        for r in plan.routes
    ]
    cache = ResponseCache(cache_dir) if ai.cache and cache_dir is not None else None
    return RoutedLLM(routes, policy=policy, budget=budget, record_usage=record_usage, cache=cache)
