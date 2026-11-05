"""Planner Agent: specification + architecture → backlog (ТЗ §6, FR-03).

Inputs: ``requirements`` and ``architecture``. Output: backlog items with
dependencies, estimates and priorities, grouped into milestones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates, find_cycle
from ai_coding.domain.enums import AgentName


class PlannerAgent(LLMAgent):
    """Breaks the design into an ordered, estimated backlog."""

    name = AgentName.PLANNER
    title = "Planner Agent"
    mission = (
        "You plan delivery: small backlog items with acceptance checks, explicit "
        "dependencies, story-point estimates and priorities, grouped into milestones."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("requirements", ctx.inputs.get("requirements")),
            ("architecture", ctx.inputs.get("architecture")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Plan the MVP. Items must be small (at most 8 points), reference the "
            "requirements they deliver and the module they touch, and depend only on "
            "earlier work. Include tests and documentation work. Every functional "
            "requirement must be delivered by at least one item, and every item must "
            "belong to exactly one milestone."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        problems: list[str] = []
        items = output.get("items", [])
        ids = [i["id"] for i in items]
        problems += [f"duplicate item id {d}" for d in duplicates(ids)]
        known = set(ids)
        modules = {m["name"] for m in (ctx.inputs.get("architecture") or {}).get("modules", [])}
        frs = [
            r["id"]
            for r in (ctx.inputs.get("requirements") or {}).get("functional_requirements", [])
        ]
        delivered: set[str] = set()
        for item in items:
            for dep in item["depends_on"]:
                if dep not in known:
                    problems.append(f"{item['id']} depends on unknown item {dep}")
                elif dep == item["id"]:
                    problems.append(f"{item['id']} depends on itself")
            if modules and item["module"] not in modules:
                problems.append(f"{item['id']} touches unknown module {item['module']}")
            for ref in item["requirement_refs"]:
                delivered.add(ref)
                if frs and ref not in frs:
                    problems.append(f"{item['id']} references unknown requirement {ref}")
        cycle = find_cycle({i["id"]: i["depends_on"] for i in items})
        if cycle:
            problems.append("dependency cycle: " + " -> ".join(cycle))
        problems += [f"{fr} is not delivered by any item" for fr in frs if fr not in delivered]
        placed: dict[str, int] = {}
        for milestone in output.get("milestones", []):
            for ref in milestone["items"]:
                if ref not in known:
                    problems.append(f"milestone {milestone['name']} lists unknown item {ref}")
                placed[ref] = placed.get(ref, 0) + 1
        problems += [f"{i} is in several milestones" for i, n in placed.items() if n > 1]
        problems += [f"{i} is in no milestone" for i in ids if i not in placed]
        return problems


def render_markdown(plan: Mapping[str, Any]) -> str:
    """Backlog as Markdown (``docs/backlog.md``)."""
    total = sum(i["estimate_points"] for i in plan["items"])
    lines = ["# Backlog", "", f"Всего: {len(plan['items'])} задач, {total} story points.", ""]
    by_id = {i["id"]: i for i in plan["items"]}
    for milestone in plan["milestones"]:
        points = sum(by_id[i]["estimate_points"] for i in milestone["items"] if i in by_id)
        lines += [f"## {milestone['name']} ({points} SP)", "", milestone["goal"], "",
                  "| ID | Задача | Тип | Модуль | Требования | Зависит от | SP | Приоритет |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
        for ref in milestone["items"]:
            i = by_id.get(ref)
            if i is None:
                continue
            lines.append(
                f"| {i['id']} | {_cell(i['title'])} | {i['type']} | `{i['module']}` | "
                f"{', '.join(i['requirement_refs']) or '—'} | "
                f"{', '.join(i['depends_on']) or '—'} | "
                f"{i['estimate_points']} | {i['priority']} |"
            )
        lines.append("")
    lines += ["## Критерии приёмки задач", ""]
    for i in plan["items"]:
        lines += [f"### {i['id']} — {i['title']}", "", i["description"], ""]
        lines += [f"- [ ] {a}" for a in i["acceptance"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


class MigrationPlannerAgent(PlannerAgent):
    """MODERNIZE role of the Planner: impact analysis and a stepwise plan (FR-05).

    Inputs: ``goal``, ``files`` (repository paths), ``context`` (relevant files),
    ``baseline`` (test result before any change). Each step must be small enough for
    one reviewable PR and carry its own verification and rollback.
    """

    schema_name = "planner.migration"
    title = "Planner Agent (modernization)"
    mission = (
        "You plan a modernization (refactoring, dependency upgrade or migration) as a "
        "sequence of small, independently verifiable and reversible steps, after an "
        "impact analysis of the current code."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("goal", ctx.inputs.get("goal")),
            ("repository_files", ctx.inputs.get("files")),
            ("relevant_files", ctx.inputs.get("context")),
            ("baseline_tests", ctx.inputs.get("baseline")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Analyse the impact of the goal on the repository, then split the work into "
            "at most 20 steps. Each step: one small PR that keeps the tests green, lists "
            "the files it touches, how to verify it and how to roll it back."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        problems: list[str] = []
        existing = set(ctx.inputs.get("files") or [])
        steps = output.get("steps", [])
        problems += [f"duplicate step id {d}" for d in duplicates(s["id"] for s in steps)]
        if existing:
            problems += [
                f"impact lists {f}, which does not exist"
                for f in output.get("impact", {}).get("files", [])
                if f not in existing
            ]
        for step in steps:
            if len(step["files"]) > 25:
                problems.append(f"{step['id']} touches {len(step['files'])} files: split it")
        return problems
