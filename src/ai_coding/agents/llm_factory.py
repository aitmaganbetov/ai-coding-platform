"""Build the routed LLM an agent uses from the project configuration (FR-15).

Chain = the agent's route (``ai.routing[agent]``) or the default model, followed by
``ai.fallback`` (duplicates removed). API keys are read from the environment by the
*name* given in the configuration (``api_key_env``), then from :data:`CREDENTIALS` — keys
the server keeps for providers configured in the Web UI (ADR-0046), resolved on demand
and never put into ``os.environ`` (no subprocess or sandbox inherits them). A route whose
key is missing is skipped and reported, never called without credentials.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ai_coding.config.models import AICodingConfig, ModelRoute
from ai_coding.domain.enums import AgentName, NetworkPolicy, ProviderName
from ai_coding.llm.cache import ResponseCache
from ai_coding.llm.guard import Budget, PolicyGate, UsageRecorder
from ai_coding.llm.providers import OLLAMA_BASE_URL, make_provider
from ai_coding.llm.router import Route, RoutedLLM
from ai_coding.security.endpoints import LocalOnlyTransport, Resolver, resolves_locally


class Credentials:
    """Keys resolved by the server itself, by the name a route gives in ``api_key_env``.

    A source is a callable (e.g. decrypting a project's secret) called when a key is
    needed; a failing source counts as a missing key.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sources: dict[str, Callable[[], str | None]] = {}

    def register(self, name: str, source: Callable[[], str | None]) -> None:
        with self._lock:
            self._sources[name] = source

    def unregister(self, name: str) -> None:
        with self._lock:
            self._sources.pop(name, None)

    def lookup(self, name: str) -> str | None:
        with self._lock:
            source = self._sources.get(name)
        if source is None:
            return None
        try:
            return source() or None
        except Exception:  # an unreadable key is a missing key, reported as such
            return None


CREDENTIALS = Credentials()


def api_key(env: Mapping[str, str], name: str | None) -> str | None:
    """The key a route names: the environment first, then the server's credentials."""
    if not name:
        return None
    return env.get(name) or CREDENTIALS.lookup(name)


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
        if route.api_key_env and not api_key(env, route.api_key_env):
            skipped.append(f"{route.provider.value}/{route.model}: ${route.api_key_env} is not set")
            continue
        routes.append(route)
    return ChainPlan(routes=tuple(routes), skipped=tuple(skipped))


def _local_only(config: AICodingConfig, plan: ChainPlan, resolver: Resolver | None) -> ChainPlan:
    """Under ``security.network: local-only`` drop routes whose host resolves outside the
    local environment (ТЗ §12): the configuration check alone cannot see DNS."""
    if config.security.network is not NetworkPolicy.LOCAL_ONLY:
        return plan
    routes, skipped = [], list(plan.skipped)
    for route in plan.routes:
        url = route.base_url or (OLLAMA_BASE_URL if route.provider is ProviderName.OLLAMA else "")
        ok, reason = resolves_locally(url, resolver) if url else (False, "no endpoint")
        if ok:
            routes.append(route)
        else:
            skipped.append(f"{route.provider.value}/{route.model}: local-only policy: {reason}")
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
    resolver: Resolver | None = None,
) -> RoutedLLM:
    """Routed, guarded LLM for ``agent``; raises :class:`LLMUnavailableError`."""
    env = os.environ if env is None else env
    plan = _local_only(config, plan_chain(config, agent, env), resolver)
    if not plan.routes:
        raise LLMUnavailableError("no usable model route: " + "; ".join(plan.skipped))
    ai = config.ai
    if config.security.network is NetworkPolicy.LOCAL_ONLY:
        # Checked again on every request, against the address actually connected to.
        transport = LocalOnlyTransport(transport, resolver)
    pricing = {m: (p.input_per_mtok, p.output_per_mtok) for m, p in ai.pricing.items()}
    routes = [
        Route(
            make_provider(
                r.provider,
                base_url=r.base_url,
                api_key=api_key(env, r.api_key_env),
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
