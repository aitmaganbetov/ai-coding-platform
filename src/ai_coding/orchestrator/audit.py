"""Audit Logger: append-only audit events with redaction (ТЗ §3, FR-16, NFR-07)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from ai_coding.domain.enums import Outcome
from ai_coding.observability import get_logger
from ai_coding.persistence.models import AuditEvent
from ai_coding.security import redact

_log = get_logger("ai_coding.audit")
_MAX_DETAIL_CHARS = 16_000


def _sanitize(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    text = json.dumps(details, default=str, ensure_ascii=False)
    text = redact(text)
    if len(text) > _MAX_DETAIL_CHARS:
        return {"truncated": True, "preview": text[:_MAX_DETAIL_CHARS]}
    parsed: dict[str, Any] = json.loads(text)
    return parsed


class AuditLogger:
    """Writes audit events in the caller's transaction (atomic with the change audited)."""

    def record(
        self,
        session: Session,
        *,
        actor: str,
        action: str,
        outcome: Outcome,
        project_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        target: str | None = None,
        details: Mapping[str, Any] | None = None,
        actor_role: str | None = None,
    ) -> AuditEvent:
        """Add an audit event to ``session``."""
        event = AuditEvent(
            actor=actor,
            actor_role=actor_role,
            project_id=project_id,
            run_id=run_id,
            action=action,
            target=redact(target) if target else None,
            outcome=outcome.value,
            details=_sanitize(details),
        )
        session.add(event)
        _log.info(
            "audit",
            action=action,
            outcome=outcome.value,
            actor=actor,
            project_id=str(project_id) if project_id else None,
            run_id=str(run_id) if run_id else None,
            target=event.target,
        )
        return event
