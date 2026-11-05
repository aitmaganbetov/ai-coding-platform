"""Test doubles for orchestrator tests. Test-only: never imported by production code."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_coding.config.generator import render_initial_config
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import Mode, TaskPhase
from ai_coding.orchestrator import HandlerRegistry, Orchestrator, StepContext, StepResult
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.retry import RetryPolicy
from ai_coding.persistence import Database, upgrade_to_head

Behaviour = Callable[[StepContext], Awaitable[StepResult]]
OPEN_DATABASES: list[Database] = []


@dataclass
class ScriptedHandler:
    """Handler whose behaviour per call is scripted by the test."""

    name: str
    capabilities: frozenset[str]
    behaviour: Behaviour | None = None
    cost_usd: float = 0.0
    available: bool = True
    calls: list[str] = field(default_factory=list)
    concurrent: int = 0
    max_concurrent: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_available(self) -> bool:
        return self.available

    async def run(self, ctx: StepContext) -> StepResult:
        with self._lock:
            self.calls.append(ctx.task_key)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.behaviour is None:
                await asyncio.sleep(0)
                return StepResult(output={"key": ctx.task_key, "deps": sorted(ctx.dependencies)})
            return await self.behaviour(ctx)
        finally:
            with self._lock:
                self.concurrent -= 1


def handler(
    name: str, *caps: str, behaviour: Behaviour | None = None, **kw: Any
) -> ScriptedHandler:
    return ScriptedHandler(
        name=name, capabilities=frozenset(caps or {name}), behaviour=behaviour, **kw
    )


def make_config(mode: Mode = Mode.DEVELOP, **overrides: Any) -> AICodingConfig:
    _, base = render_initial_config(name="demo", mode=mode)
    data = base.model_dump(mode="json")
    for dotted, value in overrides.items():
        target = data
        parts = dotted.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return AICodingConfig.model_validate(data)


def make_orchestrator(
    root: Path,
    handlers: list[Any],
    *,
    config: AICodingConfig | None = None,
    db: Database | None = None,
    **kw: Any,
) -> Orchestrator:
    root.mkdir(parents=True, exist_ok=True)
    if db is None:
        db = Database.for_project(root)
        upgrade_to_head(db)
        OPEN_DATABASES.append(db)
    kw.setdefault("retry_policy", RetryPolicy(base_delay=0.0, jitter=False))
    kw.setdefault("poll_interval", 0.01)
    kw.setdefault("actor", "local:tester")
    return Orchestrator(
        db=db,
        config=config or make_config(),
        project_root=root,
        registry=HandlerRegistry(handlers),
        **kw,
    )


def step(
    key: str, cap: str | None = None, *, phase: TaskPhase = TaskPhase.EXECUTION, **kw: Any
) -> StepSpec:
    return StepSpec(key=key, capability=cap or key, phase=phase, **kw)
