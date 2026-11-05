"""Performance Agent: metrics + profiles → bottlenecks and reproducible benchmarks
(ТЗ §6, B-606).

The agent works on *supplied* measurements (load-test summaries, APM exports,
profiler output); it never invents numbers. Every bottleneck cites the metrics it
is based on and comes with a benchmark that reproduces it, so the improvement can be
verified by a deterministic run rather than by the model's opinion.

Inputs: ``metrics`` (``[{"name", "value", "unit", "baseline"?}]``), ``profiles``
(text, untrusted), ``code`` (``{path: content}``, untrusted), ``stacks``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates
from ai_coding.domain.enums import AgentName
from ai_coding.security import redact


class PerformanceAgent(LLMAgent):
    """Finds measured bottlenecks and specifies benchmarks that reproduce them."""

    name = AgentName.PERFORMANCE
    title = "Performance Agent"
    mission = (
        "You analyse performance measurements and profiles, identify bottlenecks that "
        "the data supports, and specify reproducible benchmarks (tool, command, metric, "
        "target) that demonstrate each bottleneck and verify its fix."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("stacks", ctx.inputs.get("stacks")),
            ("metrics", ctx.inputs.get("metrics")),
            ("profiles", ctx.inputs.get("profiles")),
            ("code", ctx.inputs.get("code")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Report only bottlenecks supported by the given metrics or profiles and cite "
            "the metric names. Give each bottleneck at least one benchmark with a command "
            "that runs without production access or credentials."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        metrics = {str(m["name"]) for m in ctx.inputs.get("metrics") or []}
        code: Mapping[str, str] = ctx.inputs.get("code") or {}
        bottlenecks = output.get("bottlenecks", [])
        benchmarks = output.get("benchmarks", [])
        ids = [b["id"] for b in bottlenecks]
        problems = [f"duplicate bottleneck id {i}" for i in duplicates(ids)]
        problems += [f"duplicate benchmark id {i}" for i in duplicates(b["id"] for b in benchmarks)]
        for b in bottlenecks:
            problems += [f"{b['id']} cites unknown metric {m}" for m in b["metrics"]
                         if m not in metrics]  # fmt: skip
            if b.get("file") and b["file"] not in code:
                problems.append(f"{b['id']} points to {b['file']}, which was not shown")
        measured = {b["bottleneck"] for b in benchmarks}
        problems += [f"{i} has no benchmark" for i in ids if i not in measured]
        for bench in benchmarks:
            if bench["bottleneck"] not in ids:
                problems.append(f"{bench['id']} measures unknown bottleneck {bench['bottleneck']}")
            if redact(bench["command"]) != bench["command"]:
                problems.append(f"{bench['id']}: the command contains a secret")
        return problems
