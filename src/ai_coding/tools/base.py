"""Tool adapter contract and the normalised finding model."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ai_coding.domain.enums import Severity, Stack


class ToolCategory(StrEnum):
    """What a tool checks; maps to a Quality Gate."""

    SECRETS = "secrets"
    SAST = "sast"
    LINT = "lint"
    TYPES = "types"
    DEPENDENCIES = "dependencies"
    TESTS = "tests"
    BUILD = "build"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of a tool."""

    name: str
    category: ToolCategory
    binary: str
    image: str  # digest-pinned image for the Docker sandbox (NFR-05)
    stacks: frozenset[Stack] = frozenset()  # empty: applies to every project
    executes_project_code: bool = False  # True for test runners / builds
    homepage: str = ""
    env: tuple[tuple[str, str], ...] = ()  # e.g. disable telemetry / update checks


@dataclass(frozen=True, slots=True)
class Finding:
    """A tool finding normalised across tools (FR-06: severity and evidence)."""

    tool: str
    rule_id: str
    severity: Severity
    title: str
    message: str
    path: str | None
    line: int | None
    fingerprint: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "tool": self.tool,
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        """Inverse of :meth:`to_dict`."""
        return cls(
            tool=str(data["tool"]),
            rule_id=str(data["rule_id"]),
            severity=Severity(data["severity"]),
            title=str(data["title"]),
            message=str(data.get("message", "")),
            path=data.get("path"),
            line=data.get("line"),
            fingerprint=str(data["fingerprint"]),
            evidence=dict(data.get("evidence") or {}),
        )


def fingerprint(tool: str, rule_id: str, path: str | None, anchor: str) -> str:
    """Stable identity of a finding.

    ``anchor`` is content-based (the matched code line, or a hash of a secret), not a
    line number, so the fingerprint survives unrelated edits that shift lines; the
    baseline (ТЗ §10) matches on it.
    """
    normalized = " ".join(anchor.split())
    data = "\x1f".join([tool, rule_id, path or "", normalized])
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolRun:
    """Result of running one tool."""

    tool: str
    category: ToolCategory
    version: str | None
    backend: str
    exit_code: int
    succeeded: bool  # the tool ran and produced a parsable result
    findings: tuple[Finding, ...]
    duration_seconds: float
    error: str | None = None
    raw_output: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolRun:
        """Inverse of :meth:`to_dict`."""
        return cls(
            tool=str(data["tool"]),
            category=ToolCategory(data["category"]),
            version=data.get("version"),
            backend=str(data["backend"]),
            exit_code=int(data["exit_code"]),
            succeeded=bool(data["succeeded"]),
            findings=tuple(Finding.from_dict(f) for f in data.get("findings", [])),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            error=data.get("error"),
            raw_output=str(data.get("raw_output", "")),
        )

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """JSON form (raw output only on request)."""
        data: dict[str, Any] = {
            "tool": self.tool,
            "category": self.category.value,
            "version": self.version,
            "backend": self.backend,
            "exit_code": self.exit_code,
            "succeeded": self.succeeded,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "findings": [f.to_dict() for f in self.findings],
        }
        if include_raw:
            data["raw_output"] = self.raw_output
        return data


@runtime_checkable
class ToolAdapter(Protocol):
    """Contract of a tool integration: command line in, normalised findings out."""

    spec: ToolSpec

    def version_args(self) -> Sequence[str]:
        """Arguments (after the binary) that print the version."""
        ...

    def parse_version(self, output: str) -> str | None:
        """Extract a version string."""
        ...

    def scan_args(self, target: str, *, extra: dict[str, str]) -> Sequence[str]:
        """Arguments (after the binary) to scan ``target`` read-only, JSON to stdout."""
        ...

    def ok_exit_codes(self) -> frozenset[int]:
        """Exit codes that mean "scan completed" (with or without findings)."""
        ...

    def parse(self, stdout: str, *, target: str) -> list[Finding]:
        """Turn tool output into findings with repository-relative paths."""
        ...


def relative_path(path: str | None, target: str) -> str | None:
    """Strip the scan root from a tool-reported path (sandbox or host)."""
    if not path:
        return None
    norm = path.replace("\\", "/")
    root = target.replace("\\", "/").rstrip("/")
    if root and norm.startswith(root + "/"):
        norm = norm[len(root) + 1 :]
    return norm.removeprefix("./")
