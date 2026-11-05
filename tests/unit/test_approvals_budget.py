"""Approval Manager (roles, TTL, idempotency) and budget tracker."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from ai_coding.config.models import BudgetsConfig
from ai_coding.domain.enums import ApprovalOperation, ApprovalStatus, Role
from ai_coding.orchestrator.approval import ApprovalManager
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.budget import BudgetTracker
from ai_coding.orchestrator.errors import ApprovalDeniedError, ApprovalError, BudgetExceededError
from ai_coding.persistence import Database
from ai_coding.persistence.models import Approval, AuditEvent, Project, Run
from ai_coding.persistence.types import utcnow


@pytest.fixture
def run_id(db: Database) -> uuid.UUID:
    with db.session() as s:
        p = Project(name="p")
        s.add(p)
        s.flush()
        r = Run(project_id=p.id, mode="develop", state="PLANNED", requested_by="t")
        s.add(r)
        s.flush()
        return r.id


def _request(db: Database, run_id: uuid.UUID, mgr: ApprovalManager) -> uuid.UUID:
    with db.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        return mgr.request(
            s, run, ApprovalOperation.MERGE, reason="merge PR", requested_by="bot"
        ).id


def test_request_is_idempotent(db: Database, run_id: uuid.UUID) -> None:
    mgr = ApprovalManager(AuditLogger())
    assert _request(db, run_id, mgr) == _request(db, run_id, mgr)
    with db.session() as s:
        assert mgr.summary(s, run_id)[ApprovalStatus.PENDING] == 1


@pytest.mark.parametrize("role", [Role.OWNER, Role.MAINTAINER])
def test_approver_roles(db: Database, run_id: uuid.UUID, role: Role) -> None:
    mgr = ApprovalManager(AuditLogger())
    aid = _request(db, run_id, mgr)
    with db.session() as s:
        decided = mgr.decide(s, aid, approve=True, actor="alice", role=role, note="ok")
        assert decided.status == "approved" and decided.decided_role == role.value
    with pytest.raises(ApprovalError, match="already approved"), db.session() as s:
        mgr.decide(s, aid, approve=False, actor="alice", role=role)


@pytest.mark.parametrize("role", [Role.DEVELOPER, Role.AUDITOR, Role.AGENT])
def test_non_approver_roles_denied(db: Database, run_id: uuid.UUID, role: Role) -> None:
    mgr = ApprovalManager(AuditLogger())
    aid = _request(db, run_id, mgr)
    with pytest.raises(ApprovalDeniedError, match="cannot decide"), db.session() as s:
        mgr.decide(s, aid, approve=True, actor="mallory", role=role)
    with db.session() as s:
        assert s.get(Approval, aid).status == "pending"  # type: ignore[union-attr]


def test_reject(db: Database, run_id: uuid.UUID) -> None:
    mgr = ApprovalManager(AuditLogger())
    aid = _request(db, run_id, mgr)
    with db.session() as s:
        mgr.decide(s, aid, approve=False, actor="o", role=Role.OWNER)
    with db.session() as s:
        assert mgr.summary(s, run_id)[ApprovalStatus.REJECTED] == 1
        actions = {e.action for e in s.query(AuditEvent)}
        assert {"approval.request", "approval.reject"} <= actions


def test_expiry(db: Database, run_id: uuid.UUID) -> None:
    mgr = ApprovalManager(AuditLogger(), ttl=timedelta(seconds=-1))
    aid = _request(db, run_id, mgr)
    with db.session() as s:
        assert mgr.expire_overdue(s, run_id) == 1
        assert s.get(Approval, aid).status == "expired"  # type: ignore[union-attr]
    aid2 = _request(db, run_id, mgr)
    with pytest.raises(ApprovalError, match="expired"), db.session() as s:
        mgr.decide(s, aid2, approve=True, actor="o", role=Role.OWNER)


def test_unknown_approval(db: Database) -> None:
    with pytest.raises(ApprovalError, match="not found"), db.session() as s:
        ApprovalManager(AuditLogger()).decide(
            s, uuid.uuid4(), approve=True, actor="o", role=Role.OWNER
        )


# ---------------------------------------------------------------- budget --
def test_budget_limits() -> None:
    tracker = BudgetTracker(BudgetsConfig(max_tokens_per_run=100, max_cost_usd_per_run=1.0))
    tracker.check(extra_tokens=100)
    tracker.add(tokens=60, cost_usd=0.5)
    with pytest.raises(BudgetExceededError, match="token budget"):
        tracker.check(extra_tokens=41)
    with pytest.raises(BudgetExceededError, match="cost budget"):
        tracker.check(extra_cost_usd=0.51)
    snap = tracker.snapshot()
    assert (snap.tokens, snap.cost_usd) == (60, 0.5)
    with pytest.raises(ValueError, match="negative"):
        tracker.add(tokens=-1)


def test_budget_time_limit_and_restored_usage() -> None:
    limits = BudgetsConfig(max_duration_minutes=1)
    with pytest.raises(BudgetExceededError, match="time budget"):
        BudgetTracker(limits, started_at=utcnow() - timedelta(minutes=2)).check()
    restored = BudgetTracker(BudgetsConfig(max_tokens_per_run=10), tokens=10)
    with pytest.raises(BudgetExceededError):
        restored.check(extra_tokens=1)
