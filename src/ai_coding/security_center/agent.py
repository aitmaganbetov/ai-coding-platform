"""The Security Agent of a run, seen safely: progress, verdicts and extra risks.

The agent (``audit.triage``, opt-in when the audit starts) triages the scanners'
findings; its verdicts are advisory and never change the Quality Gates. Its text comes
from a model that read untrusted code: every string is redacted and bounded here and
rendered as text. The agent's ``evidence`` field, which tends to quote code, is withheld
like the scanners' code snippets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ai_coding.security_center.findings import multiline, text

CAPABILITY = "audit.triage"
VERDICTS = ("confirmed", "false_positive", "needs_review")
_SEVERITIES = ("critical", "high", "medium", "low", "info")
_TEXT_LIMIT = 2000
_MAX_RISKS = 20
# Fixed codes for an agent that could not run; its own error text stays on the server.
_FAILURES = (
    ("ai is disabled", "ai_disabled"),
    ("is not set", "ai_key_missing"),
    ("no usable model route", "ai_unavailable"),
)


def triage_key(fingerprint: str) -> str:
    """The id the audit gives a finding when it asks the agent (``workflows.audit``)."""
    return fingerprint[:12]


def state(
    task_status: str | None,
    output: Mapping[str, Any] | None,
    *,
    awaiting_approval: bool,
) -> dict[str, Any]:
    """``not_requested``, ``awaiting_approval``, ``running``, ``completed``, ``skipped``,
    ``error`` or ``not_run``, with counts once the agent has answered."""
    if task_status is None:
        return {"state": "not_requested", "reason": None}
    if task_status in {"PENDING", "RUNNING"}:
        return {"state": "awaiting_approval" if awaiting_approval else "running", "reason": None}
    if task_status != "SUCCEEDED":
        found, reason = _UNFINISHED.get(task_status, ("not_run", "cancelled"))
        return {"state": found, "reason": reason}
    return _answer(output if isinstance(output, Mapping) else {})


_UNFINISHED = {"FAILED": ("error", "task_failed"), "SKIPPED": ("skipped", "task_skipped")}


def _answer(out: Mapping[str, Any]) -> dict[str, Any]:
    """A finished agent step: what it concluded, or why it could not."""
    status = out.get("status")
    if status == "skipped":
        return {"state": "skipped", "reason": "no_new_findings"}
    if status != "done" or not isinstance(out.get("result"), Mapping):
        low = str(out.get("reason") or "").lower()
        code = next((c for needle, c in _FAILURES if needle in low), "agent_failed")
        return {"state": "error", "reason": code}
    result = out["result"]
    found = assessments(out)
    return {
        "state": "completed",
        "reason": None,
        "verdicts": {v: sum(1 for a in found.values() if a["verdict"] == v) for v in VERDICTS},
        "triaged": out.get("triaged") if isinstance(out.get("triaged"), int) else len(found),
        "total": out.get("total") if isinstance(out.get("total"), int) else None,
        "models": [m for m in (text(x, 100) for x in _list(out.get("models"))[:5]) if m],
        "summary": multiline(result.get("summary"), _TEXT_LIMIT),
        "additional_risks": risks(result.get("additional_risks")),
    }


def assessments(output: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Valid assessments by finding id; anything malformed is ignored."""
    result = (output or {}).get("result") if isinstance(output, Mapping) else None
    items = result.get("assessments") if isinstance(result, Mapping) else None
    found: dict[str, dict[str, Any]] = {}
    for item in _list(items):
        if not isinstance(item, Mapping) or item.get("verdict") not in VERDICTS:
            continue
        key = item.get("finding_id")
        if isinstance(key, str) and key and key not in found:
            found[key] = dict(item)
    return found


def view(assessment: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the details dialog shows of one assessment (never its code evidence)."""
    if not assessment:
        return None
    severity = assessment.get("severity")
    cwe = text(assessment.get("cwe"), 20)
    return {
        "verdict": assessment.get("verdict"),
        "severity": severity if severity in _SEVERITIES else None,
        "exploit_scenario": multiline(assessment.get("exploit_scenario"), _TEXT_LIMIT),
        "recommendation": multiline(assessment.get("recommendation"), _TEXT_LIMIT),
        "cwe": cwe if cwe and cwe.startswith("CWE-") else None,
    }


def risks(items: object) -> list[dict[str, Any]]:
    """Problems the agent noticed beyond the scanners (bounded, redacted)."""
    out = []
    for item in _list(items)[:_MAX_RISKS]:
        if not isinstance(item, Mapping):
            continue
        severity = item.get("severity")
        line = item.get("line")
        out.append(
            {
                "file": text(item.get("file"), 300),
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "severity": severity if severity in _SEVERITIES else "info",
                "description": multiline(item.get("message"), 1000),
                "recommendation": multiline(item.get("recommendation"), 1000),
            }
        )
    return out


def _list(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else []
