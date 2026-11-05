"""Reviewer Agent: diff + standards → findings with file/line/severity (ТЗ §6, §10).

The review is *advisory*: it never replaces deterministic gates or the required human
approval (ТЗ §10). Findings may block only under an explicit, approved policy
(``quality_gates.review.ai_review_blocking``).

Inputs: ``issue``, ``diff`` (unified diff, untrusted), ``changed_files``
(``{path: line_count}`` of the new versions), ``tests`` (sandbox test result),
``standards`` (project conventions).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent
from ai_coding.domain.enums import AgentName

BLOCKING_SEVERITIES = frozenset({"critical", "high"})


class ReviewerAgent(LLMAgent):
    """Reviews a change like a senior engineer, citing exact lines."""

    name = AgentName.REVIEWER
    title = "Reviewer Agent"
    mission = (
        "You review a code change for correctness, security, tests, performance and "
        "maintainability. Every finding points to a file and line of the new version and "
        "explains the problem and a concrete fix. You do not report style nits as bugs."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("issue", ctx.inputs.get("issue")),
            ("diff", ctx.inputs.get("diff")),
            ("changed_files_line_counts", ctx.inputs.get("changed_files")),
            ("test_result", ctx.inputs.get("tests")),
            ("standards", ctx.inputs.get("standards")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Review the diff. Report only real problems, ordered by severity; use "
            "'critical'/'high' only for defects that would break behaviour, lose data or "
            "create a vulnerability. Choose verdict 'request_changes' only when such a "
            "finding exists, 'approve' when there are no findings above 'low'."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        problems: list[str] = []
        files: Mapping[str, int] = ctx.inputs.get("changed_files") or {}
        for f in output.get("findings", []):
            if f["file"] not in files:
                problems.append(f"finding on {f['file']}, which is not part of the change")
            elif f["line"] > max(files[f["file"]], 1):
                problems.append(
                    f"finding on {f['file']}:{f['line']} beyond its {files[f['file']]} lines"
                )
        blocking = [f for f in output.get("findings", []) if f["severity"] in BLOCKING_SEVERITIES]
        if output.get("verdict") == "request_changes" and not blocking:
            problems.append("verdict 'request_changes' needs a critical or high finding")
        if output.get("verdict") == "approve" and blocking:
            problems.append("verdict 'approve' contradicts critical/high findings")
        return problems


def blocking_findings(review: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Critical/high findings (input to the fix loop; advisory for the gate)."""
    return [f for f in review.get("findings", []) if f["severity"] in BLOCKING_SEVERITIES]
