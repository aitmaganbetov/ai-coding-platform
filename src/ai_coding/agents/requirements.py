"""Requirements Agent: idea + constraints → requirements specification (ТЗ §6, FR-03).

Inputs (``AgentContext.inputs``):

* ``idea`` — free-text description of the product (required);
* ``constraints`` — optional list of hard constraints;
* ``previous`` — the previous version of the specification (when revising);
* ``answers`` — ``{question_id: answer}`` for the previous version's open questions.

The output is the specification itself; open questions drive the requirements
dialogue: the user answers them and a new version is produced (FR-03).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates
from ai_coding.domain.enums import AgentName


class RequirementsAgent(LLMAgent):
    """Turns an idea into a structured, traceable specification."""

    name = AgentName.REQUIREMENTS
    title = "Requirements Agent"
    mission = (
        "You turn a product idea into a precise requirements specification: goals, "
        "scope limits, functional and non-functional requirements, user stories with "
        "testable acceptance criteria, assumptions and open questions."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("idea", ctx.inputs.get("idea")),
            ("constraints", ctx.inputs.get("constraints")),
            ("previous_specification", ctx.inputs.get("previous")),
            ("answers_to_open_questions", ctx.inputs.get("answers")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        if ctx.inputs.get("previous"):
            return (
                "Revise the previous specification: apply the answers, keep ids of "
                "unchanged items stable, remove answered questions, and add new open "
                "questions only for genuinely new uncertainty."
            )
        return (
            "Write the specification for the idea. Keep it to an MVP. Every user story "
            "must reference the functional requirements it implements; acceptance "
            "criteria must be verifiable by a test. List open questions with the "
            "default assumption you used."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        problems: list[str] = []
        frs = [r["id"] for r in output.get("functional_requirements", [])]
        for label, ids in (
            ("functional requirement", frs),
            ("non-functional requirement",
             [r["id"] for r in output.get("non_functional_requirements", [])]),
            ("user story", [s["id"] for s in output.get("user_stories", [])]),
            ("open question", [q["id"] for q in output.get("open_questions", [])]),
        ):  # fmt: skip
            problems += [f"duplicate {label} id {d}" for d in duplicates(ids)]
        known = set(frs)
        covered: set[str] = set()
        for story in output.get("user_stories", []):
            for ref in story["requirement_refs"]:
                covered.add(ref)
                if ref not in known:
                    problems.append(f"{story['id']} references unknown requirement {ref}")
        problems += [f"{fr} is not covered by any user story" for fr in frs if fr not in covered]
        return problems


def render_markdown(spec: Mapping[str, Any], *, version: str | None = None) -> str:
    """Specification as Markdown (``docs/requirements.md``)."""
    lines = [f"# {spec['title']}", ""]
    if version:
        lines += [f"Версия требований: `{version}`", ""]
    lines += [spec["summary"], "", "## Цели", ""]
    lines += [f"- {g}" for g in spec["goals"]]
    if spec.get("non_goals"):
        lines += ["", "## Вне рамок", ""] + [f"- {g}" for g in spec["non_goals"]]
    if spec.get("constraints"):
        lines += ["", "## Ограничения", ""] + [f"- {c}" for c in spec["constraints"]]
    lines += ["", "## Функциональные требования", "", "| ID | Требование | Приоритет |",
              "| --- | --- | --- |"]  # fmt: skip
    lines += [
        f"| {r['id']} | {_cell(r['text'])} | {r['priority']} |"
        for r in spec["functional_requirements"]
    ]
    if spec.get("non_functional_requirements"):
        lines += ["", "## Нефункциональные требования", "", "| ID | Категория | Требование |",
                  "| --- | --- | --- |"]  # fmt: skip
        lines += [
            f"| {r['id']} | {r['category']} | {_cell(r['text'])} |"
            for r in spec["non_functional_requirements"]
        ]
    lines += ["", "## Пользовательские истории", ""]
    for s in spec["user_stories"]:
        lines += [
            f"### {s['id']} ({', '.join(s['requirement_refs'])})",
            "",
            f"Как **{s['as_a']}**, я хочу {s['i_want']}, чтобы {s['so_that']}.",
            "",
            "Критерии приёмки:",
            "",
        ]
        lines += [f"- [ ] {c}" for c in s["acceptance_criteria"]]
        lines.append("")
    if spec.get("assumptions"):
        lines += ["## Допущения", ""] + [f"- {a}" for a in spec["assumptions"]] + [""]
    if spec.get("open_questions"):
        lines += ["## Открытые вопросы", ""]
        lines += [
            f"- **{q['id']}** {q['question']} — по умолчанию: {q['default_assumption']}"
            for q in spec["open_questions"]
        ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
