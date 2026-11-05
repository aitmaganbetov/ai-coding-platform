"""Architect Agent: specification → ADRs, modules, API and data model (ТЗ §6).

Inputs: ``requirements`` (approved specification), optional ``current_state``
(discovery report of an existing repository).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates, find_cycle
from ai_coding.domain.enums import AgentName


class ArchitectAgent(LLMAgent):
    """Designs a modular architecture traceable to the requirements."""

    name = AgentName.ARCHITECT
    title = "Architect Agent"
    mission = (
        "You design the architecture of the specified system: the technology stack, "
        "modules with clear responsibilities and acyclic dependencies, architecture "
        "decision records, the public API and the data model."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("requirements", ctx.inputs.get("requirements")),
            ("current_state", ctx.inputs.get("current_state")),
            ("preferred_stack", ctx.inputs.get("preferred_stack")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Design the MVP architecture. Prefer a simple modular monolith unless a "
            "requirement demands otherwise. Every functional requirement must be "
            "assigned to at least one module. Record each significant decision as an "
            "ADR with alternatives considered. Module names are snake_case."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        problems: list[str] = []
        modules = output.get("modules", [])
        names = [m["name"] for m in modules]
        problems += [f"duplicate module {d}" for d in duplicates(names)]
        problems += [
            f"duplicate ADR id {d}" for d in duplicates(a["id"] for a in output.get("adrs", []))
        ]
        known = set(names)
        for m in modules:
            for dep in m["depends_on"]:
                if dep == m["name"]:
                    problems.append(f"module {dep} depends on itself")
                elif dep not in known:
                    problems.append(f"module {m['name']} depends on unknown module {dep}")
        cycle = find_cycle({m["name"]: m["depends_on"] for m in modules})
        if cycle:
            problems.append("module dependency cycle: " + " -> ".join(cycle))
        for endpoint in output.get("api", []):
            if endpoint["module"] not in known:
                problems.append(f"API {endpoint['path']} belongs to unknown module")
        spec = ctx.inputs.get("requirements") or {}
        frs = [r["id"] for r in spec.get("functional_requirements", [])]
        assigned = {ref for m in modules for ref in m["requirement_refs"]}
        problems += [
            f"module references unknown requirement {r}" for r in sorted(assigned - set(frs))
        ]
        problems += [f"{fr} is not assigned to any module" for fr in frs if fr not in assigned]
        return problems


def adr_documents(architecture: Mapping[str, Any]) -> dict[str, str]:
    """``docs/adr/NNNN-slug.md`` → Markdown for every ADR."""
    docs: dict[str, str] = {}
    for adr in architecture["adrs"]:
        number = adr["id"].split("-", 1)[1]
        slug = _slug(adr["title"])
        body = [
            f"# {adr['id']}: {adr['title']}",
            "",
            "- Статус: Принято",
            "",
            "## Контекст",
            adr["context"],
            "",
            "## Решение",
            adr["decision"],
            "",
            "## Последствия",
            adr["consequences"],
        ]
        if adr.get("alternatives"):
            body += ["", "## Альтернативы", *[f"- {a}" for a in adr["alternatives"]]]
        docs[f"docs/adr/{number}-{slug}.md"] = "\n".join(body) + "\n"
    return docs


def render_markdown(architecture: Mapping[str, Any]) -> str:
    """Architecture overview (``docs/architecture.md``)."""
    stack = architecture["stack"]
    lines = [
        "# Архитектура",
        "",
        f"- Язык: {stack['language']}",
        f"- Фреймворк: {stack['framework']}",
        f"- Хранилище: {stack['database']}",
    ]
    if stack.get("notes"):
        lines += ["", stack["notes"]]
    lines += ["", "## Модули", "", "| Модуль | Ответственность | Зависит от | Требования |",
              "| --- | --- | --- | --- |"]  # fmt: skip
    for m in architecture["modules"]:
        lines.append(
            f"| `{m['name']}` | {_cell(m['responsibility'])} | "
            f"{', '.join(m['depends_on']) or '—'} | {', '.join(m['requirement_refs']) or '—'} |"
        )
    lines += ["", "```mermaid", "flowchart LR"]
    for m in architecture["modules"]:
        lines.append(f"    {m['name']}")
        lines += [f"    {m['name']} --> {dep}" for dep in m["depends_on"]]
    lines += ["```"]
    if architecture.get("api"):
        lines += [
            "",
            "## API",
            "",
            "| Метод | Путь | Модуль | Описание |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {e['method']} | `{e['path']}` | `{e['module']}` | {_cell(e['summary'])} |"
            for e in architecture["api"]
        ]
    if architecture.get("data_model"):
        lines += ["", "## Модель данных"]
        for entity in architecture["data_model"]:
            lines += ["", f"### {entity['entity']}", "", entity["description"], "",
                      "| Поле | Тип | Обязательное |", "| --- | --- | --- |"]  # fmt: skip
            lines += [
                f"| {f['name']} | {f['type']} | {'да' if f['required'] else 'нет'} |"
                for f in entity["fields"]
            ]
    lines += ["", "## Решения (ADR)", ""]
    lines += [
        f"- [{a['id']}: {a['title']}](adr/{a['id'].split('-', 1)[1]}-{_slug(a['title'])}.md)"
        for a in architecture["adrs"]
    ]
    return "\n".join(lines) + "\n"


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isascii() and c.isalnum() else "-" for c in text)
    slug = "-".join(part for part in out.split("-") if part)[:60].strip("-")
    return slug or "decision"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
