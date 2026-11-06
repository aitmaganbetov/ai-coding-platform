"""Opt-in run-scoped Security Center read model using existing project RBAC."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from ai_coding.api.core import NotFoundError, Principal, ServerCore
from ai_coding.api.rbac import Permission
from ai_coding.domain.enums import Severity
from ai_coding.persistence.models import Finding, Run, Task
from ai_coding.security import redact
from ai_coding.security_center.coverage import coverage


class SecurityCenterService(ServerCore):
    """No writes, LLM calls, scans, migrations or raw artifact access."""

    def security_center(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.FINDINGS_VIEW)
            run = s.get(Run, run_id)
            if run is None or run.project_id != project_id:
                raise NotFoundError("run not found")
            tasks = s.scalars(select(Task).where(Task.run_id == run_id)).all()
            # RUNNING / failed / cancelled tasks cannot provide completed coverage.
            outputs = [
                t.output
                for t in tasks
                if t.status == "SUCCEEDED"
                and t.capability.startswith("scan.")
                and t.output
                and t.output.get("tool") == t.capability.removeprefix("scan.")
            ]
            scope = (Finding.project_id == project_id, Finding.run_id == run_id)
            counts = {severity.value: 0 for severity in Severity}
            counts.update(
                dict(
                    s.execute(
                        select(Finding.severity, func.count())
                        .where(*scope)
                        .group_by(Finding.severity)
                    ).all()
                )
            )
            rows = s.scalars(
                select(Finding)
                .where(*scope)
                .order_by(Finding.created_at.desc(), Finding.id.desc())
                .limit(max(1, min(limit, 500)))
                .offset(max(0, offset))
            ).all()
            return {
                "schema_version": "1",
                "project_id": str(project_id),
                "run_id": str(run_id),
                "run_state": run.state,
                "coverage": coverage(outputs),
                "summary": {"total": sum(counts.values()), "by_severity": counts},
                "findings": [_safe_finding(f) for f in rows],
                "page": {"limit": max(1, min(limit, 500)), "offset": max(0, offset)},
                "compliance": {"state": "not_assessed", "attestation": False},
                "ai_fix": {"state": "disabled", "requires_human_review": True},
            }


def _safe_finding(finding: Finding) -> dict[str, Any]:
    """Allowlist fields; secret evidence and arbitrary scanner messages stay private."""
    secret = finding.source == "gitleaks"
    return {
        "id": str(finding.id),
        "source": redact(finding.source),
        "rule_id": redact(finding.rule_id or ""),
        "severity": finding.severity,
        "title": "Secret detected" if secret else redact(finding.title),
        "file_path": redact(finding.file_path) if finding.file_path else None,
        "line": finding.line,
        "fingerprint": finding.fingerprint,
        "status": finding.status,
    }
