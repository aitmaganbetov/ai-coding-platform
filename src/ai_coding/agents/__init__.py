"""Agents (ТЗ §6; ADR-0011, ADR-0024).

S5 implements the CREATE agents — Requirements, Architect, Planner (LLM-driven) and
Bootstrap (deterministic templates). The remaining agents are catalogued with the
sprint in which they arrive (:mod:`ai_coding.agents.catalog`).
"""

from ai_coding.agents.architect import ArchitectAgent
from ai_coding.agents.base import (
    Agent,
    AgentContext,
    AgentError,
    AgentOutputError,
    LLMAgent,
    load_output_schema,
)
from ai_coding.agents.bootstrap import BootstrapAgent
from ai_coding.agents.catalog import CATALOG, AgentSpec, spec_for
from ai_coding.agents.llm_factory import LLMUnavailableError, build_llm, plan_chain
from ai_coding.agents.planner import PlannerAgent
from ai_coding.agents.requirements import RequirementsAgent

__all__ = [
    "CATALOG",
    "Agent",
    "AgentContext",
    "AgentError",
    "AgentOutputError",
    "AgentSpec",
    "ArchitectAgent",
    "BootstrapAgent",
    "LLMAgent",
    "LLMUnavailableError",
    "PlannerAgent",
    "RequirementsAgent",
    "build_llm",
    "load_output_schema",
    "plan_chain",
    "spec_for",
]
