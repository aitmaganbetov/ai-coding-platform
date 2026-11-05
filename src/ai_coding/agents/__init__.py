"""Agents (ТЗ §6; ADR-0011, ADR-0024).

S5 implements the CREATE agents — Requirements, Architect, Planner (LLM-driven) and
Bootstrap (deterministic templates); S6 adds Developer, Reviewer, Database and the
Planner's modernization role; S7 adds Security, QA, Performance, DevOps and
Maintenance (:mod:`ai_coding.agents.catalog`).
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
from ai_coding.agents.database import DatabaseAgent, is_migration_path
from ai_coding.agents.developer import DeveloperAgent
from ai_coding.agents.devops import DevOpsAgent
from ai_coding.agents.llm_factory import LLMUnavailableError, build_llm, plan_chain
from ai_coding.agents.maintenance import MaintenanceAgent
from ai_coding.agents.performance import PerformanceAgent
from ai_coding.agents.planner import MigrationPlannerAgent, PlannerAgent
from ai_coding.agents.qa import QAAgent
from ai_coding.agents.requirements import RequirementsAgent
from ai_coding.agents.reviewer import ReviewerAgent, blocking_findings
from ai_coding.agents.security import SecurityAgent

__all__ = [
    "CATALOG",
    "Agent",
    "AgentContext",
    "AgentError",
    "AgentOutputError",
    "AgentSpec",
    "ArchitectAgent",
    "BootstrapAgent",
    "DatabaseAgent",
    "DevOpsAgent",
    "DeveloperAgent",
    "LLMAgent",
    "LLMUnavailableError",
    "MaintenanceAgent",
    "MigrationPlannerAgent",
    "PerformanceAgent",
    "PlannerAgent",
    "QAAgent",
    "RequirementsAgent",
    "ReviewerAgent",
    "SecurityAgent",
    "blocking_findings",
    "build_llm",
    "is_migration_path",
    "load_output_schema",
    "plan_chain",
    "spec_for",
]
