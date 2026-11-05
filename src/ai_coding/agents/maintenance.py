"""Maintenance Agent: issues + findings + vulnerable dependencies → prioritised fix
plan with regression tests (ТЗ §6, B-607).

Every input item is either planned or explicitly deferred with a reason, so nothing
silently disappears. Dependency upgrades may only target a version a scanner reported
as fixing the advisory (the model does not invent versions), and every planned item
names the regression test that proves the fix. The plan is executed by MAINTAIN
through the usual implement loop, approval and PR.

Inputs: ``issues`` (``[{"id", "title", "body"}]``, untrusted), ``findings``
(``[{"id", "tool", "rule_id", "severity", "path", "title"}]``), ``dependencies``
(``[{"id", "package", "ecosystem", "version", "fixed_versions", "advisories"}]``),
``telemetry`` (optional error/incident summary, untrusted), ``files`` (project
file list, for path checks).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import (
    AgentContext,
    LLMAgent,
    duplicates,
    is_test_path,
    unsafe_path,
)
from ai_coding.domain.enums import AgentName

MAX_FILES_PER_ITEM = 15


class MaintenanceAgent(LLMAgent):
    """Triages maintenance work into small, verifiable changes."""

    name = AgentName.MAINTENANCE
    title = "Maintenance Agent"
    mission = (
        "You triage maintenance work — bug reports, scanner findings, vulnerable "
        "dependencies, incidents — into a prioritised plan of small, independently "
        "verifiable changes. Each change names the files it touches and the regression "
        "test that proves it."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("issues", ctx.inputs.get("issues")),
            ("scanner_findings", ctx.inputs.get("findings")),
            ("vulnerable_dependencies", ctx.inputs.get("dependencies")),
            ("telemetry", ctx.inputs.get("telemetry")),
            ("project_files", ctx.inputs.get("files")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Plan or defer every issue, finding and dependency by its id. Priorities: "
            "p0 exploitable security or data loss, p1 user-facing bugs and high "
            "vulnerabilities, p2 the rest, p3 hygiene. Upgrade a dependency only to one "
            "of its fixed_versions. Group items that touch the same code."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        known = _sources(ctx.inputs)
        deps = {str(d["id"]): d for d in ctx.inputs.get("dependencies") or []}
        files = set(ctx.inputs.get("files") or [])
        items = output.get("items", [])
        problems = [f"duplicate item id {i}" for i in duplicates(i["id"] for i in items)]
        handled: list[str] = []
        for item in items:
            problems += self._check_item(item, known, deps, files)
            handled += item["sources"]
        deferred = [d["source"] for d in output.get("deferred", [])]
        problems += [f"deferred unknown source {s}" for s in deferred if s not in known]
        both = set(handled) & set(deferred)
        problems += [f"{s} is both planned and deferred" for s in sorted(both)]
        problems += [f"{s} is neither planned nor deferred"
                     for s in known if s not in set(handled) | set(deferred)]  # fmt: skip
        return problems

    @staticmethod
    def _check_item(
        item: Mapping[str, Any], known: set[str], deps: Mapping[str, Any], files: set[str]
    ) -> list[str]:
        iid = item["id"]
        problems = [f"{iid} references unknown source {s}" for s in item["sources"]
                    if s not in known]  # fmt: skip
        for path in item["files"]:
            why = unsafe_path(path)
            if why:
                problems.append(f"{iid}: {path} {why}")
            elif files and path not in files and not is_test_path(path):
                problems.append(f"{iid}: {path} does not exist")
        if len(item["files"]) > MAX_FILES_PER_ITEM:
            problems.append(f"{iid} touches {len(item['files'])} files: split it")
        test = item["regression_test"]["file"]
        if not is_test_path(test):
            problems.append(f"{iid}: regression test {test} is not a test file")
        for upgrade in item.get("upgrades", []):
            dep = deps.get(upgrade["source"])
            if dep is None:
                problems.append(f"{iid} upgrades unknown dependency {upgrade['source']}")
            elif upgrade["to_version"] not in (dep.get("fixed_versions") or []):
                problems.append(
                    f"{iid}: {dep['package']} {upgrade['to_version']} is not a known fixed "
                    f"version ({', '.join(dep.get('fixed_versions') or []) or 'none'})"
                )
        return problems


def _sources(inputs: Mapping[str, Any]) -> set[str]:
    return {str(x["id"]) for key in ("issues", "findings", "dependencies")
            for x in inputs.get(key) or []}  # fmt: skip
