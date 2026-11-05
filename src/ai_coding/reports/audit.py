"""Audit report model and renderers (FR-06: findings with severity and evidence)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ai_coding.domain.enums import Severity
from ai_coding.security import redact

MAX_MARKDOWN_FINDINGS = 200
_ICON = {"PASS": "✅", "FAIL": "❌", "SKIPPED": "⏭️"}


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Everything an auditor needs, with links to evidence."""

    project: str
    run_id: str
    generated_at: str
    backend: str
    verdict: str
    gates: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    baseline: dict[str, Any]
    integrity: dict[str, Any]
    discovery: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    suppressions: dict[str, Any] = field(default_factory=dict)
    triage: dict[str, Any] | None = None
    reproducibility: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = "2"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (stable key order)."""
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "backend": self.backend,
            "verdict": self.verdict,
            "gates": self.gates,
            "tools": self.tools,
            "summary": self.severity_counts(),
            "findings": self.findings,
            "baseline": self.baseline,
            "integrity": self.integrity,
            "discovery": self.discovery,
            "suppressions": self.suppressions,
            "triage": self.triage,
            "reproducibility": self.reproducibility,
            "notes": self.notes,
        }

    def to_json(self) -> str:
        """Redacted JSON text."""
        return (
            redact(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=False)) + "\n"
        )

    def severity_counts(self) -> dict[str, int]:
        """Number of new (non-baselined) findings per severity."""
        counts = {s.value: 0 for s in Severity}
        for f in self.findings:
            if f.get("status") == "new":
                counts[f["severity"]] += 1
        return counts


def _cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: AuditReport) -> str:
    """Human-readable report (also suitable as a PR comment body in S4)."""
    lines = [
        f"# Audit report — {_cell(report.project)}",
        "",
        f"**Verdict: {_ICON.get(report.verdict, '')} {report.verdict}** · run `{report.run_id}` · "
        f"backend `{report.backend}` · {report.generated_at}",
        "",
        "## Quality Gates",
        "",
        "| Gate | Result | Blocking | Reason |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {g['gate']} | {_ICON.get(g['result'], '')} {g['result']} | "
        f"{'yes' if g['blocking'] else 'no'} | {_cell(g['reason'])} |"
        for g in report.gates
    )
    lines += ["", "## Tools", "", "| Tool | Version | Status | Findings | Duration |",
              "| --- | --- | --- | --- | --- |"]  # fmt: skip
    for t in report.tools:
        status = (
            f"skipped: {_cell(t.get('error'))}" if t.get("skipped")
            else "ok" if t["succeeded"] else f"error: {_cell(t.get('error'))}"
        )  # fmt: skip
        lines.append(
            f"| {t['tool']} | {t.get('version') or '?'} | {status} | {len(t['findings'])} | "
            f"{t['duration_seconds']}s |"
        )
    counts = report.severity_counts()
    lines += ["", "## Findings", "", "New findings by severity: "
              + ", ".join(f"{k} {v}" for k, v in counts.items()), ""]  # fmt: skip
    new = [f for f in report.findings if f.get("status") == "new"]
    order = [s.value for s in Severity]
    new.sort(key=lambda f: (order.index(f["severity"]), f.get("path") or "", f.get("line") or 0))
    if new:
        lines += [
            "| Severity | Tool | Rule | Location | Title |",
            "| --- | --- | --- | --- | --- |",
        ]
        for f in new[:MAX_MARKDOWN_FINDINGS]:
            location = f"{f.get('path') or '-'}:{f.get('line') or '-'}"
            lines.append(
                f"| {f['severity']} | {f['tool']} | `{_cell(f['rule_id'])}` | "
                f"`{_cell(location)}` | {_cell(f['title'])} |"
            )
        if len(new) > MAX_MARKDOWN_FINDINGS:
            lines.append(f"\n… {len(new) - MAX_MARKDOWN_FINDINGS} more in the JSON report.")
    else:
        lines.append("No new findings.")
    b = report.baseline
    lines += [
        "",
        "## Baseline",
        "",
        f"Suppressed by active baseline entries: {b.get('suppressed', 0)}; "
        f"matching expired entries: {b.get('expired', 0)}.",
        "",
        "## Integrity",
        "",
        f"Repository unchanged by the audit: **{report.integrity.get('unchanged')}** "
        f"({_cell(report.integrity.get('method'))}).",
    ]
    lines += _triage_section(report) + _suppressions_section(report)
    lines += _reproducibility_section(report)
    if report.notes:
        lines += ["", "## Notes", ""] + [f"- {_cell(n)}" for n in report.notes]
    return redact("\n".join(lines)) + "\n"


def _triage_section(report: AuditReport) -> list[str]:
    triage = report.triage
    if triage is None:
        return []
    lines = ["", "## Security triage (advisory)", ""]
    if triage.get("status") != "done":
        return [*lines, f"Not available: {_cell(triage.get('reason'))}."]
    result = triage["result"]
    lines += [
        _cell(result.get("summary")),
        "",
        f"Triaged {triage.get('triaged')} of {triage.get('total')} new finding(s) with "
        f"{', '.join(triage.get('models') or []) or 'the configured model'}. The verdicts "
        "do not change the gates; dismissing a finding needs a baseline entry.",
        "",
        "| Finding | Verdict | Severity | Recommendation |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| `{_cell(a['finding_id'])}` | {a['verdict']} | {a['severity']} | "
              f"{_cell(a['recommendation'])} |" for a in result.get("assessments", [])]  # fmt: skip
    risks = result.get("additional_risks") or []
    if risks:
        lines += ["", "Additional risks noticed by the agent:", ""]
        lines += [f"- {r['severity']}: `{_cell(r['file'])}:{r.get('line') or '-'}` "
                  f"{_cell(r['message'])}" for r in risks]  # fmt: skip
    return lines


def _suppressions_section(report: AuditReport) -> list[str]:
    s = report.suppressions
    if not s:
        return []
    lines = [
        "",
        "## Tool-level suppressions",
        "",
        "These bypass the platform baseline (no owner, reason or expiry); review them.",
    ]
    files = s.get("files") or []
    inline = s.get("inline") or {}
    if not files and not inline:
        lines.append("- no ignore files or inline suppression markers found")
    lines += [f"- `{_cell(f['path'])}` ({f['tool']}): {f['entries']} entr(y/ies)" for f in files]
    lines += [f"- inline {tool} markers: {n}" for tool, n in inline.items()]
    lines += [f"- {_cell(n)}" for n in s.get("notes") or []]
    return lines


def _reproducibility_section(report: AuditReport) -> list[str]:
    if not report.reproducibility:
        return []
    lines = [
        "",
        "## Reproducibility",
        "",
        "| Tool | Version | Sandbox image | Data snapshot |",
        "| --- | --- | --- | --- |",
    ]
    for r in report.reproducibility:
        data = r.get("data") or {}
        snapshot = ", ".join(f"{k}={v}" for k, v in sorted(data.items()) if v) or "-"
        image = "not run (skipped)" if r.get("skipped") else f"`{_cell(r.get('image') or 'host')}`"
        lines.append(f"| {r['tool']} | {r.get('version') or '-'} | "
                     f"{image} | {_cell(snapshot)} |")  # fmt: skip
    return lines
