"""SARIF 2.1.0 export of an audit report (FR-10; GitHub code scanning compatible)."""

from __future__ import annotations

import json
from typing import Any

from ai_coding.security import redact

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
# GitHub uses `security-severity` (0-10) to bucket results into critical/high/medium/low.
_SECURITY_SEVERITY = {
    "critical": "9.5",
    "high": "8.0",
    "medium": "5.5",
    "low": "3.0",
    "info": "1.0",
}
_HOMEPAGES = {
    "gitleaks": "https://github.com/gitleaks/gitleaks",
    "semgrep": "https://semgrep.dev",
    "ruff": "https://docs.astral.sh/ruff/",
}


def to_sarif(report: dict[str, Any]) -> dict[str, Any]:
    """Build a SARIF log with one run per tool; baselined findings are marked as such."""
    runs = []
    for tool in report.get("tools", []):
        name = tool["tool"]
        findings = [f for f in report.get("findings", []) if f["tool"] == name]
        rules: dict[str, dict[str, Any]] = {}
        results = []
        for f in findings:
            rule_id = f["rule_id"]
            rules.setdefault(
                rule_id,
                {
                    "id": rule_id,
                    "name": rule_id,
                    "shortDescription": {"text": redact(f["title"])[:1000] or rule_id},
                    "properties": {
                        "security-severity": _SECURITY_SEVERITY[f["severity"]],
                        "tags": ["ai-coding", tool["category"]],
                    },
                },
            )
            result: dict[str, Any] = {
                "ruleId": rule_id,
                "level": _LEVEL[f["severity"]],
                "message": {"text": redact(f.get("message") or f["title"])[:10_000]},
                "partialFingerprints": {"aiCodingFingerprint/v1": f["fingerprint"]},
                "baselineState": "unchanged" if f.get("status") == "baselined" else "new",
                "properties": {"severity": f["severity"], "status": f.get("status", "new")},
            }
            if f.get("path"):
                location: dict[str, Any] = {
                    "artifactLocation": {"uri": f["path"], "uriBaseId": "%SRCROOT%"}
                }
                if f.get("line"):
                    location["region"] = {"startLine": int(f["line"])}
                result["locations"] = [{"physicalLocation": location}]
            results.append(result)
        driver: dict[str, Any] = {
            "name": name,
            "informationUri": _HOMEPAGES.get(
                name, "https://github.com/aitmaganbetov/ai-coding-platform"
            ),
            "rules": list(rules.values()),
        }
        if tool.get("version"):
            driver["semanticVersion"] = tool["version"]
            driver["version"] = tool["version"]
        run: dict[str, Any] = {
            "tool": {"driver": driver},
            "automationDetails": {"id": f"ai-coding/{name}/"},
            "results": results,
            "invocations": [
                {
                    "executionSuccessful": bool(tool["succeeded"]),
                    **(
                        {
                            "toolExecutionNotifications": [
                                {
                                    "level": "error",
                                    "message": {"text": redact(str(tool.get("error")))},
                                }
                            ]
                        }
                        if not tool["succeeded"]
                        else {}
                    ),
                }
            ],
        }
        runs.append(run)
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": runs}


def render_sarif(report: dict[str, Any]) -> str:
    """SARIF as JSON text."""
    return json.dumps(to_sarif(report), indent=2, ensure_ascii=False) + "\n"
