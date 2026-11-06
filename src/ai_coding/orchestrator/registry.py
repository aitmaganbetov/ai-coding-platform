"""Agent Registry: step handlers and their capabilities (FR-08).

A *handler* executes one kind of DAG step. Agents (S5+) are handlers backed by an
LLM; deterministic platform handlers (discovery, tools) use the same contract.
Third-party handlers are discovered through the ``ai_coding.handlers`` entry-point
group.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy import select

from ai_coding.orchestrator.errors import ApprovalRequiredError, PolicyDeniedError
from ai_coding.orchestrator.policy import Decision, Verdict
from ai_coding.persistence.models import Agent

if TYPE_CHECKING:
    import asyncio

    from sqlalchemy.orm import Session

    from ai_coding.config.models import AICodingConfig
    from ai_coding.domain.enums import ProviderName
    from ai_coding.llm.base import TokenUsage
    from ai_coding.orchestrator.budget import BudgetTracker
    from ai_coding.orchestrator.policy import PolicyEngine
    from ai_coding.persistence.artifacts import ArtifactRef
    from ai_coding.tools.base import Finding

ENTRY_POINT_GROUP = "ai_coding.handlers"


@dataclass(slots=True)
class StepResult:
    """What a handler returns. Status is decided by the orchestrator, not the handler."""

    output: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float | None = None


@dataclass(slots=True)
class StepContext:
    """Everything a handler may use; nothing else is reachable by design."""

    run_id: uuid.UUID
    task_id: uuid.UUID
    task_key: str
    attempt: int
    input: Mapping[str, Any]
    dependencies: Mapping[str, Mapping[str, Any]]
    project_root: Path
    config: AICodingConfig
    policy: PolicyEngine
    cancel_event: asyncio.Event
    effect: Callable[..., dict[str, Any]]
    save_artifact: Callable[[str, bytes, str], ArtifactRef]
    log: Any
    budget: BudgetTracker
    record_usage: Callable[[ProviderName, str, TokenUsage, float], None]
    record_findings: Callable[[Sequence[Finding]], int]
    project_id: uuid.UUID | None = None
    # The tree a server run examines when it is not the project's own: a snapshot of a
    # user's workspace (ADR-0038). Configuration, baseline and state stay the project's.
    source_root: Path | None = None

    @property
    def tree(self) -> Path:
        """The files the step examines (scanners, stacks, context)."""
        return self.source_root or self.project_root

    @property
    def cancelled(self) -> bool:
        """Cooperative cancellation flag."""
        return self.cancel_event.is_set()

    def authorize(self, decision: Decision) -> None:
        """Enforce a policy decision inside a handler.

        ``DENY`` raises :class:`PolicyDeniedError`; ``REQUIRE_APPROVAL`` raises
        :class:`ApprovalRequiredError` so the orchestrator pauses the run for a human.
        """
        if decision.verdict is Verdict.ALLOW:
            return
        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            raise ApprovalRequiredError(
                f"{decision.reason} (rule {decision.rule})", operation=decision.operation
            )
        raise PolicyDeniedError(f"{decision.reason} (rule {decision.rule})")


@runtime_checkable
class StepHandler(Protocol):
    """Contract every handler implements."""

    name: str
    capabilities: frozenset[str]
    cost_usd: float

    def is_available(self) -> bool:
        """Whether the handler can run in this environment (binaries, providers…)."""
        ...

    async def run(self, ctx: StepContext) -> StepResult:
        """Execute the step."""
        ...


class HandlerRegistry:
    """In-memory catalogue of handlers, optionally mirrored to the ``agents`` table."""

    def __init__(self, handlers: Iterable[StepHandler] = ()) -> None:
        self._handlers: dict[str, StepHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: StepHandler) -> None:
        """Add a handler; names are unique."""
        if not isinstance(handler, StepHandler):
            raise TypeError(f"{handler!r} does not implement StepHandler")
        if not handler.name or not handler.capabilities:
            raise ValueError("handler needs a name and at least one capability")
        if handler.name in self._handlers:
            raise ValueError(f"handler '{handler.name}' is already registered")
        self._handlers[handler.name] = handler

    def load_entry_points(self) -> list[str]:
        """Register handlers advertised by installed plugins; returns their names."""
        loaded: list[str] = []
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            factory = ep.load()
            handler = factory() if callable(factory) else factory
            self.register(handler)
            loaded.append(handler.name)
        return loaded

    def get(self, name: str) -> StepHandler:
        """Handler by name."""
        try:
            return self._handlers[name]
        except KeyError:
            raise KeyError(f"no handler named '{name}'") from None

    def all(self) -> list[StepHandler]:
        """All handlers sorted by name."""
        return [self._handlers[k] for k in sorted(self._handlers)]

    def for_capability(self, capability: str) -> list[StepHandler]:
        """Handlers advertising ``capability``."""
        return [h for h in self.all() if capability in h.capabilities]

    def sync_to_db(self, session: Session) -> None:
        """Upsert the catalogue into the ``agents`` table."""
        existing = {a.name: a for a in session.scalars(select(Agent))}
        for handler in self.all():
            row = existing.get(handler.name)
            caps = sorted(handler.capabilities)
            if row is None:
                session.add(
                    Agent(name=handler.name, capabilities=caps, cost_per_call_usd=handler.cost_usd)
                )
            else:
                row.capabilities = caps
                row.cost_per_call_usd = handler.cost_usd
