"""Run FSM: exhaustive transition table and StateManager semantics (NFR-06: 100% branches)."""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import select

from ai_coding.domain.enums import RunState
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.errors import ConcurrencyConflictError, InvalidTransitionError
from ai_coding.orchestrator.states import (
    ACTIVE_STATES,
    RUN_TRANSITIONS,
    TERMINAL_STATES,
    StateManager,
    can_transition,
    is_terminal,
    require_transition,
)
from ai_coding.persistence import Database
from ai_coding.persistence.models import AuditEvent, Project, Run, RunEvent
from ai_coding.persistence.types import utcnow

S = RunState
# Expected table written independently of the implementation (ТЗ §5 + ADR-0006).
EXPECTED: dict[RunState, set[RunState]] = {
    S.CREATED: {S.DISCOVERY, S.CANCELLED, S.FAILED},
    S.DISCOVERY: {S.PLANNED, S.PAUSED, S.FAILED, S.CANCELLED},
    S.PLANNED: {S.AWAITING_APPROVAL, S.RUNNING, S.FAILED, S.CANCELLED},
    S.AWAITING_APPROVAL: {S.RUNNING, S.FAILED, S.CANCELLED},
    S.RUNNING: {S.VERIFYING, S.AWAITING_APPROVAL, S.PAUSED, S.FAILED, S.CANCELLED},
    S.VERIFYING: {S.COMPLETED, S.RUNNING, S.PAUSED, S.FAILED, S.CANCELLED},
    S.PAUSED: {S.DISCOVERY, S.RUNNING, S.VERIFYING, S.FAILED, S.CANCELLED},
    S.COMPLETED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
}


def test_table_covers_every_state() -> None:
    assert set(RUN_TRANSITIONS) == set(RunState)
    assert {k: set(v) for k, v in RUN_TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize(("source", "target"), list(itertools.product(RunState, RunState)))
def test_every_pair(source: RunState, target: RunState) -> None:
    allowed = target in EXPECTED[source]
    assert can_transition(source, target) is allowed
    if allowed:
        require_transition(source, target)
    else:
        with pytest.raises(InvalidTransitionError):
            require_transition(source, target)


def test_happy_path_is_reachable() -> None:
    path = [
        S.CREATED,
        S.DISCOVERY,
        S.PLANNED,
        S.AWAITING_APPROVAL,
        S.RUNNING,
        S.VERIFYING,
        S.COMPLETED,
    ]
    for a, b in itertools.pairwise(path):
        assert can_transition(a, b)


def test_terminal_and_active_sets() -> None:
    assert TERMINAL_STATES == {S.COMPLETED, S.FAILED, S.CANCELLED}
    assert all(is_terminal(s) for s in TERMINAL_STATES)
    assert not is_terminal(S.PAUSED)
    assert ACTIVE_STATES == {S.DISCOVERY, S.RUNNING, S.VERIFYING}


def test_no_self_transitions() -> None:
    assert not any(can_transition(s, s) for s in RunState)


def test_every_non_terminal_can_fail_and_cancel() -> None:
    for state in set(RunState) - TERMINAL_STATES:
        assert can_transition(state, S.CANCELLED)
        assert can_transition(state, S.FAILED)


# ------------------------------------------------------------ StateManager --
@pytest.fixture
def run(db: Database) -> uuid.UUID:
    with db.session() as s:
        project = Project(name="p")
        s.add(project)
        s.flush()
        r = Run(project_id=project.id, mode="audit", state=S.CREATED.value, requested_by="t")
        s.add(r)
        s.flush()
        return r.id


def _transition(db: Database, run_id: uuid.UUID, target: RunState, key: str) -> RunEvent | None:
    with db.session() as s:
        r = s.get(Run, run_id)
        assert r is not None
        return StateManager(AuditLogger()).transition(
            s, r, target, actor="tester", idempotency_key=key, reason="test"
        )


def test_transition_journals_and_audits(db: Database, run: uuid.UUID) -> None:
    event = _transition(db, run, S.DISCOVERY, "k1")
    assert event is not None and event.seq == 1
    _transition(db, run, S.PLANNED, "k2")
    with db.session() as s:
        r = s.get(Run, run)
        assert r is not None
        assert r.state == "PLANNED"
        assert r.started_at is not None
        assert r.version == 3
        events = list(
            s.scalars(select(RunEvent).where(RunEvent.run_id == run).order_by(RunEvent.seq))
        )
        assert [(e.from_state, e.to_state) for e in events] == [
            ("CREATED", "DISCOVERY"),
            ("DISCOVERY", "PLANNED"),
        ]
        audits = list(s.scalars(select(AuditEvent).where(AuditEvent.run_id == run)))
        assert {a.target for a in audits} == {"CREATED→DISCOVERY", "DISCOVERY→PLANNED"}
        assert all(a.actor == "tester" and a.outcome == "success" for a in audits)


def test_replay_with_same_key_is_noop(db: Database, run: uuid.UUID) -> None:
    assert _transition(db, run, S.DISCOVERY, "same") is not None
    assert _transition(db, run, S.DISCOVERY, "same") is None
    with db.session() as s:
        assert len(list(s.scalars(select(RunEvent).where(RunEvent.run_id == run)))) == 1


def test_replay_with_other_target_rejected(db: Database, run: uuid.UUID) -> None:
    _transition(db, run, S.DISCOVERY, "k")
    with pytest.raises(InvalidTransitionError, match="was used for"):
        _transition(db, run, S.CANCELLED, "k")


def test_invalid_transition_rejected(db: Database, run: uuid.UUID) -> None:
    with pytest.raises(InvalidTransitionError):
        _transition(db, run, S.COMPLETED, "k")


def test_terminal_sets_finished_and_clears_lease(db: Database, run: uuid.UUID) -> None:
    with db.session() as s:
        r = s.get(Run, run)
        assert r is not None
        r.lease_owner = "w"
        r.lease_expires_at = utcnow()
    _transition(db, run, S.CANCELLED, "c")
    with db.session() as s:
        r = s.get(Run, run)
        assert r is not None
        assert r.finished_at is not None
        assert r.lease_owner is None and r.lease_expires_at is None


def test_concurrent_modification_detected(db: Database, run: uuid.UUID) -> None:
    manager = StateManager(AuditLogger())
    s1 = db._sessions()
    s2 = db._sessions()
    try:
        r1 = s1.get(Run, run)
        r2 = s2.get(Run, run)
        assert r1 is not None and r2 is not None
        manager.transition(s1, r1, S.DISCOVERY, actor="a", idempotency_key="a")
        s1.commit()
        with pytest.raises(ConcurrencyConflictError):
            manager.transition(s2, r2, S.CANCELLED, actor="b", idempotency_key="b")
    finally:
        s1.close()
        s2.rollback()
        s2.close()


def test_state_before_pause(db: Database, run: uuid.UUID) -> None:
    with db.session() as s:
        r = s.get(Run, run)
        assert r is not None
        assert StateManager.state_before_pause(s, r) is None
    _transition(db, run, S.DISCOVERY, "1")
    _transition(db, run, S.PAUSED, "2")
    with db.session() as s:
        r = s.get(Run, run)
        assert r is not None
        assert StateManager.state_before_pause(s, r) is S.DISCOVERY
