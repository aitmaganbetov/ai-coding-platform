"""Trivy adapter: dependency vulnerabilities and IaC misconfigurations, offline.

The vulnerability DB is downloaded once by ``ai-coding tools pull`` into the platform
cache (``trivy/db``); scans run with ``--offline-scan --skip-db-update`` and no
network. The DB version (``UpdatedAt``) is recorded in the report: the same project
and the same DB snapshot always give the same findings (reproducibility, NFR-05).
Secrets are left to Gitleaks (``--scanners vuln,misconfig``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import Severity
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path

TRIVY_IMAGE = (  # aquasec/trivy 0.62.1 (Docker Hub, via mirror.gcr.io)
    "mirror.gcr.io/aquasec/trivy@sha256:fc10faf341a1d8fa8256c5ff1a6662ef74dd38b65034c8ce42346cf958a02d5d"
)
DB_REPOSITORY = "mirror.gcr.io/aquasec/trivy-db:2"
_SEVERITY = {"CRITICAL": Severity.CRITICAL, "HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM,
             "LOW": Severity.LOW}  # fmt: skip


class TrivyAdapter:
    """``trivy fs`` offline, JSON on stdout, never updates anything during a scan."""

    spec = ToolSpec(
        name="trivy",
        category=ToolCategory.DEPENDENCIES,
        binary="trivy",
        image=TRIVY_IMAGE,
        homepage="https://trivy.dev/",
        env=(("TRIVY_NO_PROGRESS", "true"), ("TRIVY_DISABLE_VEX_NOTICE", "true")),
    )
    cache_name = "trivy"

    # ----------------------------------------------------------- offline DB --
    def cache_ready(self, cache: Path) -> bool:
        return (cache / "db" / "trivy.db").is_file()

    def cache_metadata(self, cache: Path) -> Mapping[str, Any]:
        try:
            meta = json.loads((cache / "db" / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {"db_version": meta.get("Version"), "db_updated_at": meta.get("UpdatedAt"),
                "db_repository": DB_REPOSITORY}  # fmt: skip

    def prepare_args(self, cache: str) -> Sequence[str]:
        """Networked DB download (``ai-coding tools pull``)."""
        return ["image", "--download-db-only", "--cache-dir", cache,
                "--db-repository", DB_REPOSITORY, "--no-progress"]  # fmt: skip

    # ----------------------------------------------------------------- scan --
    def version_args(self) -> Sequence[str]:
        return ["--version"]

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"Version:\s*v?(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def scan_args(self, target: str, *, extra: dict[str, str]) -> Sequence[str]:
        return [
            "fs", "--cache-dir", extra["cache"], "--skip-db-update", "--skip-java-db-update",
            "--offline-scan", "--skip-check-update", "--cache-backend", "memory",
            "--scanners", "vuln,misconfig", "--format", "json", "--quiet", "--exit-code", "0",
            target,
        ]  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0})

    def parse(self, stdout: str, *, target: str) -> list[Finding]:
        data = json.loads(stdout or "{}")
        if not isinstance(data, dict):
            raise TypeError("trivy output is not a JSON object")
        findings: list[Finding] = []
        for result in data.get("Results") or []:
            path = relative_path(result.get("Target"), target)
            for v in result.get("Vulnerabilities") or []:
                vid, pkg = str(v["VulnerabilityID"]), str(v.get("PkgName", ""))
                installed = str(v.get("InstalledVersion", ""))
                fixed = v.get("FixedVersion") or None
                title = str(v.get("Title") or vid)
                findings.append(Finding(
                    tool="trivy", rule_id=vid,
                    severity=_SEVERITY.get(str(v.get("Severity", "")).upper(), Severity.INFO),
                    title=f"{pkg} {installed}: {vid}"[:200],
                    message=f"{title}" + (f" (fixed in {fixed})" if fixed else " (no fix yet)"),
                    path=path, line=None,
                    fingerprint=fingerprint("trivy", vid, path, f"{pkg}@{installed}"),
                    evidence={"package": pkg, "installed": installed, "fixed": fixed,
                              "url": v.get("PrimaryURL"), "class": result.get("Class")},
                ))  # fmt: skip
            for m in result.get("Misconfigurations") or []:
                if str(m.get("Status", "FAIL")).upper() != "FAIL":
                    continue
                mid = str(m.get("ID") or m.get("AVDID"))
                line = (m.get("CauseMetadata") or {}).get("StartLine")
                message = str(m.get("Message") or m.get("Description") or "")
                findings.append(Finding(
                    tool="trivy", rule_id=mid,
                    severity=_SEVERITY.get(str(m.get("Severity", "")).upper(), Severity.INFO),
                    title=f"{mid}: {m.get('Title', '')}"[:200], message=message,
                    path=path, line=int(line) if line else None,
                    fingerprint=fingerprint("trivy", mid, path, message),
                    evidence={"url": m.get("PrimaryURL"), "resolution": m.get("Resolution"),
                              "class": result.get("Class")},
                ))  # fmt: skip
        return findings
