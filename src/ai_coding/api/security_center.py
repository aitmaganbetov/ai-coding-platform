"""Opt-in run-scoped Security Center read model using existing project RBAC.

Every call checks ``findings.view`` in the project on the server (the role capped by
the token, the token's project scope, no implicit access for instance administrators)
and that the run, and the finding, belong to that project. Nothing here writes, scans,
calls an LLM or reads artifacts.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_coding.api.core import NotFoundError, Principal, ServerCore
from ai_coding.api.rbac import Permission
from ai_coding.api.runs import GATES_TASK
from ai_coding.domain.enums import Severity
from ai_coding.persistence.models import Finding, Run, Task
from ai_coding.security_center import findings as safe
from ai_coding.security_center.coverage import ScanTask, coverage

SCHEMA_VERSION = "2"
MAX_LIMIT = 500
FINISHED = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


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
        severity: str | None = None,
        source: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Coverage, counts of the whole run and one filtered page of safe findings."""
        limit, offset = max(1, min(limit, MAX_LIMIT)), max(0, offset)
        with self.session() as s:
            run = self._security_run(s, principal, project_id, run_id)
            tasks = s.execute(
                select(Task.capability, Task.status, Task.output).where(
                    Task.run_id == run_id, Task.capability.startswith("scan.")
                )
            ).all()
            scope = (Finding.project_id == project_id, Finding.run_id == run_id)
            filters = [
                *scope,
                *([Finding.severity == severity] if severity else []),
                *([Finding.source == source] if source else []),
                *([Finding.status == status] if status else []),
            ]
            by_severity = {level.value: 0 for level in Severity}
            by_severity.update(_counts(s, Finding.severity, scope))
            rows = s.scalars(
                select(Finding)
                .where(*filters)
                .order_by(Finding.created_at.desc(), Finding.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            matching = s.scalar(select(func.count()).select_from(Finding).where(*filters))
            return {
                "schema_version": SCHEMA_VERSION,
                "project_id": str(project_id),
                "run_id": str(run_id),
                "run": {
                    "state": run.state,
                    "finished": run.state in FINISHED,
                    "mode": run.mode,
                    "kind": (run.options or {}).get("kind"),
                    "created_at": run.created_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                },
                "run_state": run.state,
                "coverage": coverage(
                    [
                        ScanTask(capability.removeprefix("scan."), task_status, output)
                        for capability, task_status, output in tasks
                    ]
                ),
                "security_gate": _security_gate(s, run_id),
                "summary": {
                    "total": sum(by_severity.values()),
                    "by_severity": by_severity,
                    "by_source": _counts(s, Finding.source, scope),
                    "by_status": _counts(s, Finding.status, scope),
                },
                "findings": [safe.row(f) for f in rows],
                "page": {
                    "limit": limit,
                    "offset": offset,
                    "total": int(matching or 0),
                    "filters": {"severity": severity, "source": source, "status": status},
                },
                "capabilities": {
                    "dast": "not_implemented",
                    "container": "not_implemented",
                },
                "compliance": {"state": "not_assessed", "attestation": False},
                "ai_fix": {"state": "disabled", "requires_human_review": True},
            }

    def security_center_finding(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        finding_id: uuid.UUID,
    ) -> dict[str, Any]:
        """One finding of the run with the allowlisted facts of its category."""
        with self.session() as s:
            self._security_run(s, principal, project_id, run_id)
            finding = s.get(Finding, finding_id)
            if finding is None or finding.project_id != project_id or finding.run_id != run_id:
                raise NotFoundError("finding not found")
            return safe.details(finding)

    def _security_run(
        self, s: Session, principal: Principal, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> Run:
        self.require(s, principal, project_id, Permission.FINDINGS_VIEW)
        run = s.get(Run, run_id)
        if run is None or run.project_id != project_id:
            raise NotFoundError("run not found")
        return run


def _counts(s: Session, column: Any, scope: tuple[Any, ...]) -> dict[str, int]:
    rows = s.execute(select(column, func.count()).where(*scope).group_by(column)).all()
    return {str(key): int(n) for key, n in rows if key is not None}


def _security_gate(s: Session, run_id: uuid.UUID) -> dict[str, Any] | None:
    """The run's ``security`` Quality Gate: result and blocking only. Its reason can
    quote a tool's error, so it stays on the run page under ``run.view``."""
    output = s.scalar(select(Task.output).where(Task.run_id == run_id, Task.key == GATES_TASK))
    gates = output.get("gates") if isinstance(output, dict) else None
    for gate in gates if isinstance(gates, list) else []:
        if isinstance(gate, dict) and gate.get("gate") == "security":
            result = gate.get("result")
            return {
                "result": result if result in {"PASS", "FAIL", "SKIPPED"} else "UNKNOWN",
                "blocking": gate.get("blocking") is True,
            }
    return None
