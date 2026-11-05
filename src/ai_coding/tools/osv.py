"""OSV-Scanner adapter: dependency vulnerabilities from the OSV database, offline.

Offline databases (one zip per ecosystem) are downloaded by ``ai-coding tools pull``
into the platform cache by OSV-Scanner itself, using a seed directory with one
lockfile per supported ecosystem; scans then use ``--offline-vulnerabilities``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import Severity
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path

# google/osv-scanner v2.6.0. v2.0.x panics while downloading the offline DB since OSV
# published Drupal advisories (ecosystem "Packagist:https://packages.drupal.org/8").
OSV_IMAGE = (
    "ghcr.io/google/osv-scanner"
    "@sha256:afd838850ac1a0fcc15ff4a041dc9ba11123c3f0d2666217a5f0fcf9222b55fa"
)
CACHE_ENV = "OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"
_LEVELS = {"CRITICAL": Severity.CRITICAL, "HIGH": Severity.HIGH, "MODERATE": Severity.MEDIUM,
           "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}  # fmt: skip
# Minimal lockfiles: make the downloader fetch the DB of every common ecosystem.
SEED_FILES: Mapping[str, str] = {
    "package-lock.json": json.dumps({"name": "seed", "lockfileVersion": 3, "packages": {
        "": {"name": "seed"}, "node_modules/lodash": {"version": "4.17.21"}}}),
    "requirements.txt": "requests==2.32.3\n",
    "composer.lock": json.dumps({"packages": [{"name": "monolog/monolog", "version": "3.7.0"}],
                                 "packages-dev": []}),
    "go.mod": "module seed\n\ngo 1.22\n\nrequire golang.org/x/text v0.21.0\n",
    "Cargo.lock": 'version = 3\n\n[[package]]\nname = "serde"\nversion = "1.0.210"\n'
                  'source = "registry+https://github.com/rust-lang/crates.io-index"\n',
}  # fmt: skip


def _severity(score: str | None, label: str | None) -> Severity:
    if label and label.upper() in _LEVELS:
        return _LEVELS[label.upper()]
    try:
        value = float(score or "")
    except ValueError:
        return Severity.MEDIUM  # unscored advisory: never silently "info"
    if value >= 9.0:
        return Severity.CRITICAL
    if value >= 7.0:
        return Severity.HIGH
    if value >= 4.0:
        return Severity.MEDIUM
    return Severity.LOW if value > 0 else Severity.INFO


def _fixed(vuln: Mapping[str, Any]) -> str | None:
    for affected in vuln.get("affected") or []:
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                if event.get("fixed"):
                    return str(event["fixed"])
    return None


class OSVScannerAdapter:
    """``osv-scanner scan source --offline-vulnerabilities``."""

    spec = ToolSpec(
        name="osv-scanner",
        category=ToolCategory.DEPENDENCIES,
        binary="osv-scanner",
        image=OSV_IMAGE,
        homepage="https://google.github.io/osv-scanner/",
    )
    cache_name = "osv-scanner"
    seed_files = SEED_FILES
    # Image layouts differ between releases (v2.0.x copied the binary into the base
    # image's WORKDIR, v2.6 into /): start whichever executable exists.
    sandbox_entry = ("sh", "-c", 'for p in /osv-scanner /go/osv-scanner /root/osv-scanner '
                     '"$(command -v osv-scanner)"; do [ -x "$p" ] && exec "$p" "$@"; done; '
                     'echo "osv-scanner binary not found in the image" >&2; exit 127',
                     "osv-scanner")  # fmt: skip

    def cache_ready(self, cache: Path) -> bool:
        return any(cache.rglob("*.zip"))

    def cache_metadata(self, cache: Path) -> Mapping[str, Any]:
        zips = sorted(cache.rglob("*.zip"))
        newest = max((z.stat().st_mtime for z in zips), default=0.0)
        return {"ecosystems": sorted(z.parent.name for z in zips), "db_downloaded_at": newest}

    def scan_env(self, extra: Mapping[str, str]) -> Mapping[str, str]:
        return {CACHE_ENV: extra["cache"]} if "cache" in extra else {}

    def prepare_args(self, cache: str, seed: str) -> Sequence[str]:
        """Networked download of the offline databases (``ai-coding tools pull``)."""
        return ["scan", "source", "--offline-vulnerabilities", "--download-offline-databases",
                "--no-call-analysis=all", "--format", "json", "--recursive", seed]  # fmt: skip

    def version_args(self) -> Sequence[str]:
        return ["--version"]

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"version:?\s*v?(\d+\.\d+\.\d+)", output, re.I)
        return match.group(1) if match else None

    def scan_args(self, target: str, *, extra: dict[str, str]) -> Sequence[str]:
        # Call analysis would need the project's toolchains (Go, Cargo) in this image.
        return ["scan", "source", "--offline-vulnerabilities", "--no-call-analysis=all",
                "--format", "json", "--recursive", target]  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1, 128})  # 1: vulnerabilities found, 128: no packages found

    def parse(self, stdout: str, *, target: str) -> list[Finding]:
        if not stdout.strip():
            return []  # exit 128: nothing to scan
        data = json.loads(stdout)
        findings: list[Finding] = []
        for result in data.get("results") or []:
            path = relative_path((result.get("source") or {}).get("path"), target)
            for pkg in result.get("packages") or []:
                info = pkg.get("package") or {}
                name, version = str(info.get("name", "")), str(info.get("version", ""))
                scores = {i: g.get("max_severity") for g in pkg.get("groups") or []
                          for i in g.get("ids") or []}  # fmt: skip
                for v in pkg.get("vulnerabilities") or []:
                    vid = str(v["id"])
                    label = (v.get("database_specific") or {}).get("severity")
                    fixed = _fixed(v)
                    findings.append(Finding(
                        tool="osv-scanner", rule_id=vid,
                        severity=_severity(scores.get(vid), label),
                        title=f"{name} {version}: {vid}"[:200],
                        message=str(v.get("summary") or vid)
                        + (f" (fixed in {fixed})" if fixed else " (no fix yet)"),
                        path=path, line=None,
                        fingerprint=fingerprint("osv-scanner", vid, path, f"{name}@{version}"),
                        evidence={"package": name, "installed": version, "fixed": fixed,
                                  "ecosystem": info.get("ecosystem"),
                                  "aliases": list(v.get("aliases") or [])},
                    ))  # fmt: skip
        return findings
