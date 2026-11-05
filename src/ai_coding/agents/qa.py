"""QA Agent: requirements + code → test cases and coverage gaps (ТЗ §6, B-605).

The agent designs tests; it does not claim results. Results and coverage come only
from the sandboxed test run (``tests`` input), which the agent interprets.

Inputs: ``criteria`` (``[{"id", "text"}]`` acceptance criteria), ``code``
(``{path: content}``, untrusted), ``existing_tests`` (paths), ``tests`` (latest
sandbox result: counts and coverage, optional).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates, is_test_path, unsafe_path
from ai_coding.domain.enums import AgentName


class QAAgent(LLMAgent):
    """Turns acceptance criteria into concrete, traceable test cases."""

    name = AgentName.QA
    title = "QA Agent"
    mission = (
        "You design tests from acceptance criteria and the code under test: unit, "
        "integration and end-to-end cases with concrete inputs and expected results, "
        "traced to the criteria they verify. You point out untested code paths. You "
        "never report test results you did not receive."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("acceptance_criteria", ctx.inputs.get("criteria")),
            ("code", ctx.inputs.get("code")),
            ("existing_tests", ctx.inputs.get("existing_tests")),
            ("latest_test_run", ctx.inputs.get("tests")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Cover every acceptance criterion with at least one test case (positive and, "
            "where it matters, negative or boundary cases). Place each case in a test file "
            "following the project's conventions. List coverage gaps only for files you "
            "were shown."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        criteria = {str(c["id"]) for c in ctx.inputs.get("criteria") or []}
        code: Mapping[str, str] = ctx.inputs.get("code") or {}
        cases = output.get("test_cases", [])
        problems = [f"duplicate test case id {i}" for i in duplicates(c["id"] for c in cases)]
        covered: set[str] = set()
        for case in cases:
            for ref in case["criteria"]:
                if ref not in criteria:
                    problems.append(f"{case['id']} references unknown criterion {ref}")
                covered.add(ref)
            why = unsafe_path(case["file"])
            if why:
                problems.append(f"{case['id']}: file {case['file']} {why}")
            elif not is_test_path(case["file"]):
                problems.append(f"{case['id']}: {case['file']} is not a test file")
        problems += [f"criterion {c} has no test case" for c in sorted(criteria - covered)]
        problems += [
            f"coverage gap on {g['file']}, which was not shown"
            for g in output.get("coverage_gaps", [])
            if g["file"] not in code
        ]
        return problems
