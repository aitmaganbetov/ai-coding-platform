"""Running agents as orchestrated steps (shared by CREATE, DEVELOP, MODERNIZE, review).

:func:`run_agent` gives every agent call the same guarantees: the agent must be
enabled by policy, it gets its own routed and guarded LLM (egress, redaction,
budget, fallbacks, cache), an external provider that needs approval pauses the run
instead of silently falling back, and LLM-level errors are translated into the
orchestrator's approval / budget / permanent errors.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from ai_coding.agents import (
    Agent,
    AgentContext,
    AgentError,
    AgentOutputError,
    LLMUnavailableError,
    build_llm,
)
from ai_coding.contracts.agent import AgentResult, AgentScope, AgentTask
from ai_coding.domain.enums import ApprovalOperation
from ai_coding.llm import LLMApprovalRequiredError, LLMBudgetExceededError
from ai_coding.orchestrator.errors import (
    ApprovalRequiredError,
    BudgetExceededError,
    PermanentError,
)
from ai_coding.orchestrator.policy import Verdict
from ai_coding.orchestrator.registry import StepContext, StepResult

InputBuilder = Callable[[StepContext], dict[str, Any]]


def agent_llm(
    ctx: StepContext,
    agent: Agent,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Routed, guarded LLM for ``agent``; pauses for ``external_llm`` approval if needed."""
    try:
        llm = build_llm(
            ctx.config,
            agent.name,
            policy=ctx.policy,
            budget=ctx.budget,
            record_usage=ctx.record_usage,
            cache_dir=ctx.project_root / ".ai-coding" / "cache" / "llm",
            env=env,
            transport=transport,
        )
    except LLMUnavailableError as exc:
        raise PermanentError(f"{agent.title}: {exc}") from exc
    primary = llm.primary.provider
    decision = ctx.policy.external_llm(primary.name, base_url=primary.base_url)
    if decision.verdict is Verdict.REQUIRE_APPROVAL:
        ctx.authorize(decision)  # pause for a human instead of silently falling back
    return llm


async def run_agent(
    ctx: StepContext,
    agent: Agent,
    inputs: Mapping[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[AgentContext, AgentResult]:
    """Run ``agent`` on ``inputs`` within the step; returns its context and result."""
    ctx.authorize(ctx.policy.agent(agent.name))
    llm = agent_llm(ctx, agent, transport=transport, env=env) if agent.requires_llm else None
    agent_ctx = AgentContext(
        inputs=inputs,
        llm=llm,
        max_output_tokens=ctx.config.ai.max_output_tokens,
        temperature=ctx.config.ai.temperature,
        log=ctx.log,
    )
    task = AgentTask(
        task_id=ctx.task_id,
        agent=agent.name,
        input_refs=[f"task:{k}" for k in ctx.dependencies],
        scope=AgentScope(project_id=ctx.project_id or uuid.UUID(int=0), run_id=ctx.run_id),
        max_cost=ctx.config.budgets.max_cost_usd_per_run,
        max_duration=max(60, ctx.config.budgets.max_duration_minutes * 60),
        output_schema=dict(agent.output_schema),
    )
    try:
        result = await agent.run(task, agent_ctx)
    except LLMApprovalRequiredError as exc:
        raise ApprovalRequiredError(str(exc), ApprovalOperation.EXTERNAL_LLM) from exc
    except LLMBudgetExceededError as exc:
        raise BudgetExceededError(str(exc)) from exc
    except (AgentError, AgentOutputError) as exc:
        raise PermanentError(str(exc)) from exc
    return agent_ctx, result


class AgentStepHandler:
    """Runs one agent as a step: inputs come from the step context."""

    cost_usd = 0.0

    def __init__(
        self,
        agent: Agent,
        inputs: InputBuilder,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.agent = agent
        self.name = f"agent.{agent.name.value}"
        self.capabilities = frozenset({f"agent.{agent.name.value}"})
        self._inputs = inputs
        self._transport = transport
        self._env = env

    def is_available(self) -> bool:
        return True

    async def execute(self, ctx: StepContext) -> tuple[AgentContext, AgentResult]:
        """Run the agent; returns its context (models used) and contract result."""
        return await run_agent(
            ctx, self.agent, self._inputs(ctx), transport=self._transport, env=self._env
        )

    async def run(self, ctx: StepContext) -> StepResult:
        agent_ctx, result = await self.execute(ctx)
        return StepResult(
            output={"result": result.output, "models": list(dict.fromkeys(agent_ctx.models))},
            evidence_refs=list(result.evidence_refs),
            confidence=result.confidence,
        )
