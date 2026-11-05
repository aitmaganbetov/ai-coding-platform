"""Gitleaks adapter: secrets in the working tree (``dir``) — never stores the secret."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from ai_coding.domain.enums import Severity
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path

GITLEAKS_IMAGE = (  # v8.30.1
    "zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)


class GitleaksAdapter:
    """``gitleaks dir`` with redaction and a JSON report on stdout."""

    spec = ToolSpec(
        name="gitleaks",
        category=ToolCategory.SECRETS,
        binary="gitleaks",
        image=GITLEAKS_IMAGE,
        homepage="https://github.com/gitleaks/gitleaks",
    )

    def version_args(self) -> Sequence[str]:
        return ["version"]

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"v?(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def scan_args(self, target: str, *, extra: dict[str, str]) -> Sequence[str]:
        return [
            "dir", target,
            "--report-format", "json",
            "--report-path", "-",
            "--redact",
            "--no-banner",
            "--log-level", "error",
            "--exit-code", "0",
        ]  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0})

    def parse(self, stdout: str, *, target: str) -> list[Finding]:
        text = stdout.strip()
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            raise TypeError("gitleaks report is not a JSON array")
        findings = []
        for item in data:
            path = relative_path(item.get("File"), target)
            rule = str(item.get("RuleID") or "generic")
            # The secret itself is redacted by gitleaks; anchor on its fingerprint fields.
            anchor = hashlib.sha256(
                f"{item.get('Fingerprint', '')}|{item.get('Match', '')}".encode()
            ).hexdigest()
            findings.append(
                Finding(
                    tool="gitleaks",
                    rule_id=rule,
                    severity=Severity.HIGH,
                    title=f"Secret detected: {item.get('Description') or rule}",
                    message="A credential-like value is committed to the repository.",
                    path=path,
                    line=int(item["StartLine"]) if item.get("StartLine") else None,
                    fingerprint=fingerprint("gitleaks", rule, path, anchor),
                    evidence={"entropy": item.get("Entropy"), "match_redacted": True},
                )
            )
        return findings
