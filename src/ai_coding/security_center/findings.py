"""Safe views of persisted findings: allowlisted fields, every string redacted.

Scanner messages, raw evidence and arbitrary metadata never leave the server: they can
hold source code, secret matches or attacker-controlled text from the scanned project.
What is shown is a fixed set of fields, typed and bounded here, and the UI renders it as
text. The redactor is a heuristic, so secret findings get no title or evidence at all.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from ai_coding.security import redact

SECRET_SOURCES = frozenset({"gitleaks"})
SECRET_TITLE = "Secret detected"  # noqa: S105 - a label, not a password
# What the details view leaves out on purpose (the UI says so instead of guessing).
WITHHELD = ("scanner_message", "raw_evidence", "code_snippet", "raw_output")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}$")
_HTTPS = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[^\s<>\"'\\`]*)?$")


class FindingLike(Protocol):
    """The persisted finding columns this module reads."""

    id: Any
    source: str
    rule_id: str | None
    severity: str
    title: str
    file_path: str | None
    line: int | None
    fingerprint: str
    evidence: dict[str, Any]
    status: str
    created_at: datetime


def category(source: str, evidence: Mapping[str, Any] | None) -> str:
    """``sast``, ``sca``, ``secrets``, ``iac`` or ``other`` (lint, tests, agents)."""
    if source in SECRET_SOURCES:
        return "secrets"
    if source == "semgrep":
        return "sast"
    if source == "osv-scanner":
        return "sca"
    if source == "trivy":
        kind = (evidence or {}).get("class")
        return "iac" if kind == "config" else "secrets" if kind == "secret" else "sca"
    return "other"


def text(value: object, limit: int = 300) -> str | None:
    """A bounded, redacted, single-line string (or ``None`` for anything else)."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    raw = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(value)).strip()
    if not raw:
        return None
    out = redact(raw)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def row(finding: FindingLike) -> dict[str, Any]:
    """The table row: location and classification, no scanner message or evidence."""
    cat = category(finding.source, finding.evidence)
    return {
        "id": str(finding.id),
        "source": text(finding.source, 100) or "unknown",
        "category": cat,
        "rule_id": text(finding.rule_id, 200),
        "severity": finding.severity,
        "title": SECRET_TITLE if cat == "secrets" else (text(finding.title, 300) or "—"),
        "file_path": text(finding.file_path, 1000),
        "line": finding.line if isinstance(finding.line, int) else None,
        "fingerprint": finding.fingerprint,
        "status": text(finding.status, 20) or "open",
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
    }


def details(finding: FindingLike) -> dict[str, Any]:
    """The row plus the allowlisted facts of its category."""
    item = row(finding)
    evidence = finding.evidence if isinstance(finding.evidence, Mapping) else {}
    facts: dict[str, Any] = {}
    cat = item["category"]
    if cat == "sca":
        facts = {
            "package": text(evidence.get("package"), 200),
            "installed_version": text(evidence.get("installed"), 100),
            "fixed_version": text(evidence.get("fixed"), 200),
            "ecosystem": text(evidence.get("ecosystem"), 100),
            "aliases": _ids(evidence.get("aliases")),
            "reference": _url(evidence.get("url")),
        }
    elif cat == "iac":
        facts = {
            "resolution": text(evidence.get("resolution"), 500),
            "reference": _url(evidence.get("url")),
        }
    elif cat == "sast":
        facts = {"cwe": _labels(evidence.get("cwe"))}
    item["details"] = {k: v for k, v in facts.items() if v not in (None, [])}
    item["withheld"] = list(WITHHELD)
    return item


def _ids(value: object) -> list[str]:
    items = value if isinstance(value, list) else []
    return [v for v in items[:20] if isinstance(v, str) and _ID.match(v)]


def _labels(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [t for t in (text(v, 200) for v in items[:10]) if t]


def _url(value: object) -> str | None:
    """An advisory reference: plain ``https`` only, unchanged by redaction."""
    if not isinstance(value, str) or len(value) > 300 or not _HTTPS.match(value):
        return None
    return value if redact(value) == value else None
