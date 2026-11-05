"""Baseline of accepted existing findings (ТЗ §10).

"Baseline существующих проблем допускается с исключениями, владельцем и сроком
действия": every entry must name an owner, a reason and an expiry date. Expired
entries stop suppressing their finding. The file is committed to the repository
(``.ai-coding-baseline.json``) so that changes to it go through review.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_coding.tools.base import Finding

BASELINE_FILENAME = ".ai-coding-baseline.json"
MAX_BASELINE_BYTES = 5_000_000


class BaselineError(Exception):
    """The baseline file is unreadable or invalid."""


class BaselineEntry(BaseModel):
    """One accepted finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool: str = Field(min_length=1, max_length=100)
    rule_id: str = Field(min_length=1, max_length=200)
    path: str | None = None
    owner: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=2000)
    expires_at: date


class Baseline(BaseModel):
    """The whole baseline file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, le=1)
    entries: tuple[BaselineEntry, ...] = ()

    def partition(
        self, findings: Iterable[Finding], *, today: date | None = None
    ) -> tuple[list[Finding], list[Finding], list[Finding]]:
        """Split into ``(new, suppressed, expired)``."""
        today = today or date.today()
        active = {e.fingerprint for e in self.entries if e.expires_at >= today}
        expired_fp = {e.fingerprint for e in self.entries if e.expires_at < today}
        new: list[Finding] = []
        suppressed: list[Finding] = []
        expired: list[Finding] = []
        for finding in findings:
            if finding.fingerprint in active:
                suppressed.append(finding)
            else:
                (expired if finding.fingerprint in expired_fp else new).append(finding)
        return new, suppressed, expired

    def to_json(self) -> str:
        """Canonical, review-friendly JSON."""
        data = self.model_dump(mode="json")
        data["entries"] = sorted(
            data["entries"], key=lambda e: (e["tool"], e["path"] or "", e["rule_id"])
        )
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load_baseline(project_root: Path) -> Baseline:
    """Baseline of a project (empty if the file does not exist)."""
    path = project_root / BASELINE_FILENAME
    if not path.is_file():
        return Baseline()
    if path.stat().st_size > MAX_BASELINE_BYTES:
        raise BaselineError(f"{path} is larger than {MAX_BASELINE_BYTES} bytes")
    try:
        return Baseline.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError) as exc:
        raise BaselineError(f"{path} is invalid: {exc}") from exc


def baseline_from_findings(
    findings: Iterable[Finding], *, owner: str, reason: str, expires_at: date
) -> Baseline:
    """Baseline accepting every given finding (used to adopt an existing project)."""
    seen: dict[str, BaselineEntry] = {}
    for f in findings:
        seen.setdefault(
            f.fingerprint,
            BaselineEntry(
                fingerprint=f.fingerprint,
                tool=f.tool,
                rule_id=f.rule_id,
                path=f.path,
                owner=owner,
                reason=reason,
                expires_at=expires_at,
            ),
        )
    return Baseline(entries=tuple(seen.values()))
