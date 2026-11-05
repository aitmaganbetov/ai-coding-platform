"""Checkpoint Manager and effect ledger."""

from __future__ import annotations

import uuid

import pytest

from ai_coding.orchestrator.checkpoint import CheckpointIntegrityError, CheckpointManager
from ai_coding.orchestrator.effects import APPLIED, INTENT, EffectLedger
from ai_coding.persistence import Database
from ai_coding.persistence.models import Checkpoint, Effect, Project, Run, Task


@pytest.fixture
def run_id(db: Database) -> uuid.UUID:
    with db.session() as s:
        p = Project(name="p")
        s.add(p)
        s.flush()
        r = Run(project_id=p.id, mode="audit", state="RUNNING", requested_by="t")
        s.add(r)
        s.flush()
        s.add(Task(run_id=r.id, key="a", name="a", phase="execution", capability="c",
                   status="SUCCEEDED", output={"x": 1}))  # fmt: skip
        return r.id


def test_checkpoint_sequence_and_snapshot(db: Database, run_id: uuid.UUID) -> None:
    manager = CheckpointManager()
    with db.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert manager.latest(s, run_id) is None
        manager.save(s, run, "one")
        manager.save(s, run, "two")
    with db.session() as s:
        latest = manager.latest(s, run_id)
        assert latest is not None and latest.seq == 2 and latest.label == "two"
        assert latest.snapshot["run_state"] == "RUNNING"
        assert latest.snapshot["tasks"]["a"]["status"] == "SUCCEEDED"
        assert len(latest.snapshot["tasks"]["a"]["output_sha256"]) == 64


def test_checkpoint_corruption_detected(db: Database, run_id: uuid.UUID) -> None:
    with db.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        CheckpointManager().save(s, run, "one")
    with db.session() as s:
        cp = s.query(Checkpoint).one()
        cp.snapshot = {"run_state": "COMPLETED", "tasks": {}}
    with pytest.raises(CheckpointIntegrityError), db.session() as s:
        CheckpointManager.latest(s, run_id)


def test_effect_applied_once(db: Database, run_id: uuid.UUID) -> None:
    ledger = EffectLedger(db, run_id, None)
    calls: list[int] = []

    def action() -> dict[str, int]:
        calls.append(1)
        return {"n": len(calls)}

    assert ledger.apply("commit", action) == {"n": 1}
    assert ledger.apply("commit", action) == {"n": 1}
    assert calls == [1]
    assert ledger.status("commit") == APPLIED
    assert ledger.status("other") is None


def test_effect_recovered_by_probe_after_crash(db: Database, run_id: uuid.UUID) -> None:
    """Crash between doing the effect and recording it: the probe finds it, no redo."""
    with db.session() as s:
        s.add(Effect(run_id=run_id, key="push", status=INTENT))
    ledger = EffectLedger(db, run_id, None)
    redone: list[bool] = []
    result = ledger.apply(
        "push", lambda: redone.append(True) or {"redo": True}, probe=lambda: {"sha": "abc"}
    )
    assert result == {"sha": "abc"}
    assert redone == []
    assert ledger.status("push") == APPLIED


def test_effect_redone_when_probe_finds_nothing(db: Database, run_id: uuid.UUID) -> None:
    with db.session() as s:
        s.add(Effect(run_id=run_id, key="push", status=INTENT))
    ledger = EffectLedger(db, run_id, None)
    assert ledger.apply("push", lambda: {"done": 1}, probe=lambda: None) == {"done": 1}
    assert ledger.apply("push", lambda: {"done": 2}) == {"done": 1}


def test_effect_failure_leaves_intent(db: Database, run_id: uuid.UUID) -> None:
    ledger = EffectLedger(db, run_id, None)

    def boom() -> dict[str, int]:
        raise OSError("disk")

    with pytest.raises(OSError, match="disk"):
        ledger.apply("write", boom)
    assert ledger.status("write") == INTENT
    assert ledger.apply("write", lambda: {"ok": 1}) == {"ok": 1}


@pytest.mark.parametrize("key", ["", "x" * 301])
def test_effect_key_validation(db: Database, run_id: uuid.UUID, key: str) -> None:
    with pytest.raises(ValueError, match="effect key"):
        EffectLedger(db, run_id, None).apply(key, dict)
