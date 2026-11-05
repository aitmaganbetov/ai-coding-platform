"""Task Planner: validated DAG of steps with dependencies, criteria and budget (ТЗ §5)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ai_coding.domain.enums import ApprovalOperation, TaskPhase
from ai_coding.orchestrator.errors import PlanValidationError

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
PHASE_ORDER: Mapping[TaskPhase, int] = {
    TaskPhase.DISCOVERY: 0,
    TaskPhase.EXECUTION: 1,
    TaskPhase.VERIFICATION: 2,
}


@dataclass(frozen=True, slots=True)
class StepSpec:
    """Declarative description of one DAG step."""

    key: str
    capability: str
    name: str = ""
    phase: TaskPhase = TaskPhase.EXECUTION
    depends_on: tuple[str, ...] = ()
    input: Mapping[str, Any] = field(default_factory=dict)
    max_attempts: int = 1
    timeout_seconds: float = 1800.0
    requires_approval: ApprovalOperation | None = None
    budget_usd: float = 0.0
    success_criteria: Mapping[str, Any] | None = None  # JSON Schema the output must satisfy

    def to_dict(self) -> dict[str, Any]:
        """Canonical JSON-compatible form (used for the plan hash)."""
        return {
            "key": self.key,
            "capability": self.capability,
            "name": self.name or self.key,
            "phase": self.phase.value,
            "depends_on": sorted(self.depends_on),
            "input": dict(self.input),
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "requires_approval": self.requires_approval.value if self.requires_approval else None,
            "budget_usd": self.budget_usd,
            "success_criteria": dict(self.success_criteria) if self.success_criteria else None,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """A validated, topologically ordered DAG."""

    steps: tuple[StepSpec, ...]
    order: tuple[str, ...]
    levels: tuple[tuple[str, ...], ...]

    @property
    def total_budget_usd(self) -> float:
        """Sum of step budgets."""
        return sum(s.budget_usd for s in self.steps)

    def step(self, key: str) -> StepSpec:
        """Step by key."""
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(key)

    def digest(self) -> str:
        """Stable SHA-256 of the plan (idempotency checks)."""
        payload = json.dumps([s.to_dict() for s in self.steps], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def approvals_required(self) -> list[tuple[str, ApprovalOperation]]:
        """``(step_key, operation)`` for steps that declare an approval."""
        return [(s.key, s.requires_approval) for s in self.steps if s.requires_approval]


class TaskPlanner:
    """Validates step specs and builds an executable :class:`Plan`."""

    def __init__(self, *, max_steps: int = 500, max_budget_usd: float | None = None) -> None:
        self._max_steps = max_steps
        self._max_budget = max_budget_usd

    def build(self, steps: Sequence[StepSpec]) -> Plan:
        """Validate and order ``steps``; raises :class:`PlanValidationError`."""
        errors = list(self._validate(steps))
        if errors:
            raise PlanValidationError("; ".join(errors))
        order, levels = _topological(steps)
        plan = Plan(steps=tuple(steps), order=tuple(order), levels=levels)
        if self._max_budget is not None and plan.total_budget_usd > self._max_budget:
            raise PlanValidationError(
                f"plan budget ${plan.total_budget_usd:.2f} exceeds the run budget "
                f"${self._max_budget:.2f}"
            )
        return plan

    def _validate(self, steps: Sequence[StepSpec]) -> Iterable[str]:  # noqa: PLR0912
        if not steps:
            yield "plan has no steps"
            return
        if len(steps) > self._max_steps:
            yield f"plan has {len(steps)} steps (max {self._max_steps})"
        keys: dict[str, StepSpec] = {}
        for s in steps:
            if not _KEY_RE.match(s.key):
                yield f"invalid step key {s.key!r}"
            if s.key in keys:
                yield f"duplicate step key {s.key!r}"
            keys[s.key] = s
            if not s.capability:
                yield f"step {s.key!r} has no capability"
            if s.max_attempts < 1:
                yield f"step {s.key!r}: max_attempts must be >= 1"
            if s.timeout_seconds <= 0:
                yield f"step {s.key!r}: timeout_seconds must be > 0"
            if s.budget_usd < 0:
                yield f"step {s.key!r}: budget_usd must be >= 0"
            if s.success_criteria is not None:
                try:
                    Draft202012Validator.check_schema(dict(s.success_criteria))
                except SchemaError as exc:
                    yield f"step {s.key!r}: success_criteria is not a JSON Schema ({exc.message})"
        for s in steps:
            for dep in s.depends_on:
                if dep == s.key:
                    yield f"step {s.key!r} depends on itself"
                elif dep not in keys:
                    yield f"step {s.key!r} depends on unknown step {dep!r}"
                elif PHASE_ORDER[keys[dep].phase] > PHASE_ORDER[s.phase]:
                    yield (
                        f"step {s.key!r} ({s.phase}) cannot depend on later-phase step "
                        f"{dep!r} ({keys[dep].phase})"
                    )
        cycle = _find_cycle(steps)
        if cycle:
            yield "dependency cycle: " + " → ".join(cycle)


def _find_cycle(steps: Sequence[StepSpec]) -> list[str] | None:
    graph = {s.key: [d for d in s.depends_on if d != s.key] for s in steps}
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = grey
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == grey:
                return [*stack[stack.index(nxt) :], nxt]
            if color[nxt] == white:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = black
        return None

    for key in graph:
        if color[key] == white:
            found = visit(key)
            if found:
                return found
    return None


def _topological(steps: Sequence[StepSpec]) -> tuple[list[str], tuple[tuple[str, ...], ...]]:
    indegree = {s.key: len(set(s.depends_on)) for s in steps}
    dependents: dict[str, list[str]] = {s.key: [] for s in steps}
    for s in steps:
        for dep in set(s.depends_on):
            dependents[dep].append(s.key)
    ready = deque(sorted(k for k, d in indegree.items() if d == 0))
    order: list[str] = []
    levels: list[tuple[str, ...]] = []
    while ready:
        level = tuple(sorted(ready))
        levels.append(level)
        ready.clear()
        for key in level:
            order.append(key)
            for nxt in dependents[key]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
    return order, tuple(levels)
