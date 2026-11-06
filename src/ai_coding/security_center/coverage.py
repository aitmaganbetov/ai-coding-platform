"""Coverage is evidence of execution, never a compliance attestation.

A category is ``completed`` only when every analyser behind it ran to the end; it says
nothing about the findings (a completed scan may report many) or the Quality Gates.
Missing, skipped, cancelled, failed and still running scans never count as coverage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Existing adapters only. Trivy fs provides IaC, not container image coverage.
CAPABILITIES: Mapping[str, tuple[str, ...]] = {
    "sast": ("semgrep",),
    "sca": ("osv-scanner", "trivy"),
    "secrets": ("gitleaks",),
    "iac": ("trivy",),
    "container": (),
    "dast": (),
}

# The scanners of a security audit started from the Security Center (``audit`` kind).
SECURITY_TOOLS: tuple[str, ...] = tuple(
    dict.fromkeys(tool for tools in CAPABILITIES.values() for tool in tools)
)

# Fixed reason codes. A tool's own error text can hold paths, stderr or advisory text:
# it stays on the server, the UI explains the code instead.
_REASONS = (
    ("timed out", "timeout"),
    ("offline database missing", "offline_db_missing"),
    ("output exceeded the size limit", "output_too_large"),
    ("could not parse output", "parse_error"),
    ("docker sandbox only", "docker_only"),
    ("unavailable", "tool_unavailable"),
    ("exit code", "tool_failed"),
)
# When a tool has several tasks in one run, the least favourable state is reported.
_PRIORITY = ("error", "running", "not_run", "skipped", "completed")


@dataclass(frozen=True, slots=True)
class ScanTask:
    """A ``scan.<tool>`` task of the run: its status and persisted output."""

    tool: str
    status: str
    output: Mapping[str, Any] | None


def reason_code(error: object) -> str | None:
    """The fixed code describing a tool's error text (``other`` when unrecognised)."""
    if not isinstance(error, str) or not error.strip():
        return None
    low = error.lower()
    return next((code for needle, code in _REASONS if needle in low), "other")


def tool_state(task: ScanTask) -> tuple[str, str | None]:
    """``(state, reason)`` of one scan task; only a consistent, successful result of a
    finished task is ``completed``."""
    if task.status != "SUCCEEDED":  # PENDING, RUNNING: the run is not over yet
        return _NOT_SUCCEEDED.get(task.status, ("running", None))
    out = task.output
    if not isinstance(out, Mapping) or out.get("tool") != task.tool:
        return "error", "invalid_result"
    if out.get("skipped") is True:
        return "skipped", reason_code(out.get("error")) or "not_applicable"
    if out.get("succeeded") is True:
        return "completed", None
    return "error", reason_code(out.get("error")) or "tool_failed"


_NOT_SUCCEEDED: Mapping[str, tuple[str, str | None]] = {
    "FAILED": ("error", "task_failed"),
    "SKIPPED": ("skipped", "task_skipped"),
    "CANCELLED": ("not_run", "cancelled"),
}


def coverage(tasks: Sequence[ScanTask]) -> dict[str, Any]:
    """Per category: ``unsupported`` (no analyser in the platform), ``error``,
    ``completed`` (every analyser finished), ``skipped`` (none ran) or ``incomplete``."""
    by_tool: dict[str, tuple[str, str | None]] = {}
    for task in tasks:
        state = tool_state(task)
        known = by_tool.get(task.tool)
        if known is None or _PRIORITY.index(state[0]) < _PRIORITY.index(known[0]):
            by_tool[task.tool] = state
    result: dict[str, Any] = {}
    for name, tools in CAPABILITIES.items():
        states = [
            {"tool": tool, "state": state, "reason": reason}
            for tool in tools
            for state, reason in [by_tool.get(tool, ("not_run", "not_planned"))]
        ]
        kinds = {s["state"] for s in states}
        result[name] = {
            "available": bool(tools),
            "tools": states,
            "state": (
                "unsupported"
                if not tools
                else "error"
                if "error" in kinds
                else "completed"
                if kinds == {"completed"}
                else "skipped"
                if kinds == {"skipped"}
                else "incomplete"
            ),
        }
    return result
