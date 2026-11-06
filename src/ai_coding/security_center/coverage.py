"""Coverage is evidence of execution, never a compliance attestation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Existing adapters only. Trivy fs provides IaC, not container image coverage.
CAPABILITIES = {
    "sast": ("semgrep",),
    "sca": ("osv-scanner", "trivy"),
    "secrets": ("gitleaks",),
    "iac": ("trivy",),
    "container": (),
    "dast": (),
}


def coverage(outputs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Project persisted tool results without treating missing or failed scans as clean.

    Callers supply only completed tool tasks. A successful scan can contain findings;
    ``completed`` describes execution, not a passing Quality Gate.
    """
    by_tool = {o.get("tool"): o for o in outputs}
    result: dict[str, Any] = {}
    for name, tools in CAPABILITIES.items():
        states = []
        for tool in tools:
            output = by_tool.get(tool)
            state = "not_run"
            if output is not None:
                if output.get("skipped") is True:
                    state = "skipped"
                elif output.get("succeeded") is True:
                    state = "completed"
                else:
                    state = "error"
            states.append({"tool": tool, "state": state})
        result[name] = {
            "available": bool(tools),
            "tools": states,
            "state": (
                "unsupported"
                if not tools
                else "error"
                if any(s["state"] == "error" for s in states)
                else "completed"
                if all(s["state"] == "completed" for s in states)
                else "incomplete"
            ),
        }
    return result
