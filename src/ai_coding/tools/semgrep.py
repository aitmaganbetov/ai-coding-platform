"""Semgrep adapter: SAST with an offline, vendored ruleset by default (ADR-0019)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from ai_coding.domain.enums import Severity
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path

SEMGREP_IMAGE = (  # 1.183.0
    "semgrep/semgrep@sha256:2e45517a85e13a9cca14a2a00df6bb48affd66b8ebfae1b0991cd57578362fb0"
)
_SEVERITY = {
    "CRITICAL": Severity.CRITICAL,
    "ERROR": Severity.HIGH,
    "HIGH": Severity.HIGH,
    "WARNING": Severity.MEDIUM,
    "MEDIUM": Severity.MEDIUM,
    "INFO": Severity.LOW,
    "LOW": Severity.LOW,
    "INVENTORY": Severity.INFO,
    "EXPERIMENT": Severity.INFO,
}
REGISTRY_CONFIG = re.compile(r"^(auto|p/|r/|s/|https?://)")


def bundled_rules_dir() -> Path:
    """Directory with the platform's offline Semgrep rules."""
    return Path(str(resources.files("ai_coding.tools").joinpath("rules/semgrep")))


class SemgrepAdapter:
    """``semgrep scan`` with metrics and version checks disabled."""

    spec = ToolSpec(
        name="semgrep",
        category=ToolCategory.SAST,
        binary="semgrep",
        image=SEMGREP_IMAGE,
        homepage="https://semgrep.dev",
        env=(
            ("SEMGREP_SEND_METRICS", "off"),
            ("SEMGREP_ENABLE_VERSION_CHECK", "0"),
        ),
    )

    def version_args(self) -> Sequence[str]:
        # Without --disable-version-check, `--version` phones home and can hang offline.
        return ["--version", "--disable-version-check"]

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def scan_args(self, target: str, *, extra: dict[str, str]) -> Sequence[str]:
        config = extra.get("config", "")
        if not config:
            raise ValueError("semgrep needs a rules config path")
        return [
            "scan", "--config", config, "--json", "--metrics=off", "--disable-version-check",
            "--quiet", target,
        ]  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: findings with --error, kept for robustness

    def parse(self, stdout: str, *, target: str) -> list[Finding]:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise TypeError("semgrep output is not a JSON object")
        errors = [e for e in data.get("errors", []) if e.get("level") == "error"]
        if errors and not data.get("results"):
            raise ValueError(f"semgrep reported errors: {errors[0].get('message', '')[:300]}")
        findings = []
        for item in data.get("results", []):
            rule = str(item.get("check_id", "unknown")).rsplit(".", 1)[-1]
            extra = item.get("extra", {})
            path = relative_path(item.get("path"), target)
            line = item.get("start", {}).get("line")
            code = str(extra.get("lines", ""))
            message = str(extra.get("message", "")).strip()
            findings.append(
                Finding(
                    tool="semgrep",
                    rule_id=rule,
                    severity=_SEVERITY.get(str(extra.get("severity", "")).upper(), Severity.MEDIUM),
                    title=message.splitlines()[0][:200] if message else rule,
                    message=message,
                    path=path,
                    line=int(line) if line else None,
                    fingerprint=fingerprint("semgrep", rule, path, code or str(line)),
                    evidence={
                        "code": code[:500],
                        "cwe": extra.get("metadata", {}).get("cwe"),
                    },
                )
            )
        return findings
