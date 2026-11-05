"""Ruff adapter: Python lint (read-only: never ``--fix``)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path

RUFF_IMAGE = (  # 0.16.9-alpine
    "ghcr.io/astral-sh/ruff@sha256:8aae98e9eca908e8af82d7bfea421dd6d89f92cf2b4e3c200752f5b221f0427b"
)
_HIGH_PREFIXES = ("S",)  # flake8-bandit security rules
_MEDIUM_PREFIXES = ("F", "E9", "B", "PLE")


def _severity(code: str) -> Severity:
    if code.startswith(_HIGH_PREFIXES):
        return Severity.MEDIUM  # security lint is advisory; Semgrep owns SAST severity
    if code.startswith(_MEDIUM_PREFIXES):
        return Severity.MEDIUM
    return Severity.LOW


class RuffAdapter:
    """``ruff check --output-format json`` without fixes or cache writes."""

    spec = ToolSpec(
        name="ruff",
        category=ToolCategory.LINT,
        binary="ruff",
        image=RUFF_IMAGE,
        stacks=frozenset({Stack.PYTHON}),
        homepage="https://docs.astral.sh/ruff/",
    )

    def version_args(self) -> Sequence[str]:
        return ["--version"]

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def scan_args(self, target: str, *, extra: dict[str, str]) -> Sequence[str]:
        return [
            "check", target, "--output-format", "json", "--no-fix", "--no-cache",
            "--exit-zero", "--force-exclude",
        ]  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0})

    def parse(self, stdout: str, *, target: str) -> list[Finding]:
        data = json.loads(stdout or "[]")
        if not isinstance(data, list):
            raise TypeError("ruff output is not a JSON array")
        findings = []
        for item in data:
            code = str(item.get("code") or "syntax-error")
            path = relative_path(item.get("filename"), target)
            line = (item.get("location") or {}).get("row")
            message = str(item.get("message", ""))
            findings.append(
                Finding(
                    tool="ruff",
                    rule_id=code,
                    severity=_severity(code) if item.get("code") else Severity.HIGH,
                    title=f"{code}: {message}"[:200],
                    message=message,
                    path=path,
                    line=int(line) if line else None,
                    fingerprint=fingerprint("ruff", code, path, message),
                    evidence={"url": item.get("url")},
                )
            )
        return findings
