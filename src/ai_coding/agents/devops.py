"""DevOps Agent: build + environments → CI/CD pipeline, release plan, rollback
(ТЗ §6, B-608).

The output is a *plan* reviewed by a human; applying it goes through the normal
change flow (approval, protected branch, PR). The checks enforce the platform's
security rules on the plan itself: a valid stage graph, no secrets in commands
(secrets come from the CI secret store by reference), a manual approval before
every production deployment, and a rollback that is defined before release.

Inputs: ``stacks``, ``build`` (detected build/test commands), ``ci_files``
(``{path: content}`` of existing CI, untrusted), ``environments`` (names, e.g.
``["staging", "production"]``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates, find_cycle, unsafe_path
from ai_coding.domain.enums import AgentName
from ai_coding.security import redact

PRODUCTION = frozenset({"production", "prod", "live"})


class DevOpsAgent(LLMAgent):
    """Designs CI/CD with explicit gates, releases and rollbacks."""

    name = AgentName.DEVOPS
    title = "DevOps Agent"
    mission = (
        "You design CI/CD for the project: pipeline stages with their commands and "
        "dependencies, deployments per environment, a release plan and a rollback plan. "
        "Secrets are referenced by name from the CI secret store, never written inline. "
        "Production deployments always require a manual approval."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("stacks", ctx.inputs.get("stacks")),
            ("build", ctx.inputs.get("build")),
            ("environments", ctx.inputs.get("environments")),
            ("existing_ci", ctx.inputs.get("ci_files")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Propose the pipeline (build, test, quality gates, packaging, deployments), "
            "list the CI files to create or modify, and give a release plan and a rollback "
            "plan with a concrete trigger. Deploy only to the given environments."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        envs = {str(e) for e in ctx.inputs.get("environments") or []}
        stages = output.get("pipeline", [])
        names = [s["name"] for s in stages]
        problems = [f"duplicate stage {n}" for n in duplicates(names)]
        for stage in stages:
            problems += [f"stage {stage['name']} needs unknown stage {n}"
                         for n in stage["needs"] if n not in names]  # fmt: skip
            problems += [f"stage {stage['name']}: a command contains a secret"
                         for c in stage["commands"] if redact(c) != c][:1]  # fmt: skip
            env = stage.get("environment")
            if env is not None and env not in envs:
                problems.append(f"stage {stage['name']} deploys to unknown environment {env}")
            if env is not None and env.lower() in PRODUCTION and not stage.get("manual_approval"):
                problems.append(f"stage {stage['name']} deploys to {env} without manual approval")
        cycle = find_cycle({s["name"]: s["needs"] for s in stages})
        if cycle:
            problems.append("stage cycle: " + " → ".join(cycle))
        for f in output.get("ci_files", []):
            why = unsafe_path(f["path"])
            if why:
                problems.append(f"CI file {f['path']} {why}")
        if not output.get("rollback", {}).get("steps"):
            problems.append("the rollback plan has no steps")
        return problems
