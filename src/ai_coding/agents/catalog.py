"""Catalogue of the twelve agents of ТЗ §6 and their implementation status (B-406).

Since S7 all twelve agents are implemented; the sprint column records when each
arrived, so ``ai-coding agents list`` reports the truth instead of pretending.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_coding.agents.architect import ArchitectAgent
from ai_coding.agents.base import Agent
from ai_coding.agents.bootstrap import BootstrapAgent
from ai_coding.agents.database import DatabaseAgent
from ai_coding.agents.developer import DeveloperAgent
from ai_coding.agents.devops import DevOpsAgent
from ai_coding.agents.maintenance import MaintenanceAgent
from ai_coding.agents.performance import PerformanceAgent
from ai_coding.agents.planner import PlannerAgent
from ai_coding.agents.qa import QAAgent
from ai_coding.agents.requirements import RequirementsAgent
from ai_coding.agents.reviewer import ReviewerAgent
from ai_coding.agents.security import SecurityAgent
from ai_coding.domain.enums import AgentName


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Description of one agent (ТЗ §6 input → output)."""

    name: AgentName
    inputs: str
    outputs: str
    sprint: str
    requires_llm: bool
    factory: type | None = None

    @property
    def implemented(self) -> bool:
        """Whether the agent can run in this version."""
        return self.factory is not None

    def create(self) -> Agent:
        """New instance (only for implemented agents)."""
        if self.factory is None:
            raise LookupError(f"agent '{self.name}' is planned for {self.sprint}")
        agent: Agent = self.factory()
        return agent


CATALOG: tuple[AgentSpec, ...] = (
    AgentSpec(AgentName.REQUIREMENTS, "Описание идеи, ограничения",
              "ТЗ, user stories, acceptance criteria", "S5", True, RequirementsAgent),
    AgentSpec(AgentName.ARCHITECT, "ТЗ, текущее состояние", "ADR, схема модулей, API/DB проект",
              "S5", True, ArchitectAgent),
    AgentSpec(AgentName.PLANNER, "ТЗ, ADR", "Backlog, зависимости, оценка", "S5", True,
              PlannerAgent),
    AgentSpec(AgentName.BOOTSTRAP, "Утверждённый план", "Каркас, CI, README, конфигурация",
              "S5", False, BootstrapAgent),
    AgentSpec(AgentName.DEVELOPER, "Issue, контекст", "Patch/commit, пояснение, тесты", "S6", True,
              DeveloperAgent),
    AgentSpec(AgentName.REVIEWER, "Diff, стандарты", "Замечания с file/line/severity", "S6", True,
              ReviewerAgent),
    AgentSpec(AgentName.SECURITY, "Diff, scanner reports", "Уязвимости, evidence, рекомендации",
              "S7", True, SecurityAgent),
    AgentSpec(AgentName.QA, "Требования, код", "Тест-кейсы, результаты, coverage", "S7", True,
              QAAgent),
    AgentSpec(AgentName.DATABASE, "Schema, queries", "Анализ миграций, индексов, транзакций",
              "S6", True, DatabaseAgent),
    AgentSpec(AgentName.PERFORMANCE, "Метрики, профили",
              "Bottlenecks и воспроизводимые бенчмарки", "S7", True, PerformanceAgent),
    AgentSpec(AgentName.DEVOPS, "Сборка, окружения", "CI/CD, release plan, rollback", "S7", True,
              DevOpsAgent),
    AgentSpec(AgentName.MAINTENANCE, "Issues, telemetry", "План исправлений, проверяемые PR",
              "S7", True, MaintenanceAgent),
)  # fmt: skip


def spec_for(name: AgentName) -> AgentSpec:
    """Catalogue entry of ``name``."""
    for spec in CATALOG:
        if spec.name is name:
            return spec
    raise LookupError(name)  # pragma: no cover - CATALOG covers AgentName
