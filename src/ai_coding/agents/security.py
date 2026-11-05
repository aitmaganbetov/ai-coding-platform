"""Security Agent: scanner reports + code → triaged vulnerabilities (ТЗ §6, B-604).

The agent *triages* deterministic findings — it never creates or removes them. A
``false_positive`` verdict is advisory: the finding stays in the report and keeps
counting in the Security gate until a human accepts it in the baseline (owner,
reason, expiry; ТЗ §10). Extra risks the model notices are reported separately
and must point to code it was actually shown.

Inputs: ``findings`` (``[{"id", "tool", "rule_id", "severity", "path", "line",
"title"}]``; secret values are already redacted), ``code`` (``{path: excerpt}``,
untrusted), ``diff`` (optional, untrusted), ``stacks``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates
from ai_coding.domain.enums import AgentName

SECRET_TOOLS = frozenset({"gitleaks"})


class SecurityAgent(LLMAgent):
    """Confirms, explains and prioritises security findings with evidence."""

    name = AgentName.SECURITY
    title = "Security Agent"
    mission = (
        "You triage findings of security scanners (SAST, secrets, dependency and "
        "configuration scanners). For each finding you decide whether the code shown "
        "confirms it, explain the exploit scenario and give a concrete remediation. "
        "You never see secret values and never ask for them."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("stacks", ctx.inputs.get("stacks")),
            ("scanner_findings", ctx.inputs.get("findings")),
            ("code_excerpts", ctx.inputs.get("code")),
            ("diff", ctx.inputs.get("diff")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Assess every scanner finding exactly once by its id: 'confirmed' when the "
            "code shows the issue is reachable, 'false_positive' only with a concrete "
            "reason visible in the code, otherwise 'needs_review'. Committed secrets are "
            "always 'confirmed' (rotate, then remove from history). Add additional_risks "
            "only for problems you can point to in the code excerpts."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        findings = {str(f["id"]): f for f in ctx.inputs.get("findings") or []}
        code: Mapping[str, str] = ctx.inputs.get("code") or {}
        assessed = [a["finding_id"] for a in output.get("assessments", [])]
        problems = [f"finding {i} is assessed more than once" for i in duplicates(assessed)]
        problems += [f"assessment of unknown finding {i}" for i in assessed if i not in findings]
        problems += [f"finding {i} is not assessed" for i in findings if i not in set(assessed)]
        for a in output.get("assessments", []):
            source = findings.get(a["finding_id"])
            if (source and source.get("tool") in SECRET_TOOLS
                    and a["verdict"] == "false_positive"):  # fmt: skip
                problems.append(
                    f"finding {a['finding_id']} is a committed secret: it cannot be dismissed "
                    "by the model (a human accepts it in the baseline)"
                )
        for risk in output.get("additional_risks", []):
            if risk["file"] not in code:
                problems.append(f"additional risk on {risk['file']}, which was not shown")
            elif risk.get("line") and risk["line"] > max(len(code[risk["file"]].splitlines()), 1):
                problems.append(f"additional risk on {risk['file']}:{risk['line']} out of range")
        return problems


def confirmed(triage: Mapping[str, Any]) -> list[str]:
    """Ids of findings the agent confirmed."""
    return [a["finding_id"] for a in triage.get("assessments", []) if a["verdict"] == "confirmed"]
