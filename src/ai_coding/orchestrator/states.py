"""Run state machine (ТЗ §5; ADR-0006).

``RUN_TRANSITIONS`` is the single source of truth for allowed transitions.
Transitions are journaled in ``run_events`` in the same transaction as the state
change and are idempotent by ``(run_id, idempotency_key)``: replaying a transition
after a crash is a no-op instead of a duplicate.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from ai_coding.domain.enums import Outcome, RunState
from ai_coding.orchestrator.errors import ConcurrencyConflictError, InvalidTransitionError
from ai_coding.persistence.models import Run, RunEvent
from ai_coding.persistence.types import utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ai_coding.orchestrator.audit import AuditLogger

S = RunState

RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = MappingProxyType(
    {
        S.CREATED: frozenset({S.DISCOVERY, S.CANCELLED, S.FAILED}),
        S.DISCOVERY: frozenset({S.PLANNED, S.PAUSED, S.FAILED, S.CANCELLED}),
        S.PLANNED: frozenset({S.AWAITING_APPROVAL, S.RUNNING, S.FAILED, S.CANCELLED}),
        S.AWAITING_APPROVAL: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
        S.RUNNING: frozenset({S.VERIFYING, S.AWAITING_APPROVAL, S.PAUSED, S.FAILED, S.CANCELLED}),
        S.VERIFYING: frozenset({S.COMPLETED, S.RUNNING, S.PAUSED, S.FAILED, S.CANCELLED}),
        S.PAUSED: frozenset({S.DISCOVERY, S.RUNNING, S.VERIFYING, S.FAILED, S.CANCELLED}),
        S.COMPLETED: frozenset(),
        S.FAILED: frozenset(),
        S.CANCELLED: frozenset(),
    }
)

TERMINAL_STATES: frozenset[RunState] = frozenset(
    state for state, targets in RUN_TRANSITIONS.items() if not targets
)
ACTIVE_STATES: frozenset[RunState] = frozenset({S.DISCOVERY, S.RUNNING, S.VERIFYING})


def is_terminal(state: RunState) -> bool:
    """Whether no transition leaves ``state``."""
    return state in TERMINAL_STATES


def can_transition(source: RunState, target: RunState) -> bool:
    """Whether ``source → target`` is allowed."""
    return target in RUN_TRANSITIONS[source]


def require_transition(source: RunState, target: RunState) -> None:
    """Raise :class:`InvalidTransitionError` unless ``source → target`` is allowed."""
    if not can_transition(source, target):
        raise InvalidTransitionError(f"transition {source} → {target} is not allowed")


class StateManager:
    """Applies journaled, idempotent, optimistically-locked run transitions."""

    def __init__(self, audit: AuditLogger) -> None:
        self._audit = audit

    def transition(
        self,
        session: Session,
        run: Run,
        target: RunState,
        *,
        actor: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> RunEvent | None:
        """Move ``run`` to ``target``.

        Returns the new event, or ``None`` when the same idempotency key was already
        applied with the same target (replay after a crash). A replay with a
        different target raises :class:`InvalidTransitionError`.
        """
        existing = session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if existing.to_state != target.value:
                raise InvalidTransitionError(
                    f"idempotency key {idempotency_key!r} was used for "
                    f"→ {existing.to_state}, not → {target}"
                )
            return None

        source = RunState(run.state)
        require_transition(source, target)
        seq = (
            session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id)) or 0
        ) + 1
        now = utcnow()
        run.state = target.value
        if target in ACTIVE_STATES and run.started_at is None:
            run.started_at = now
        if is_terminal(target):
            run.finished_at = now
            run.lease_owner = None
            run.lease_expires_at = None
        event = RunEvent(
            run_id=run.id,
            seq=seq,
            from_state=source.value,
            to_state=target.value,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )
        session.add(event)
        self._audit.record(
            session,
            actor=actor,
            action="run.transition",
            outcome=Outcome.SUCCESS,
            project_id=run.project_id,
            run_id=run.id,
            target=f"{source}→{target}",
            details={"reason": reason, "seq": seq},
        )
        run_id = run.id  # read before flush: a failed flush poisons the session
        try:
            session.flush()
        except StaleDataError as exc:
            raise ConcurrencyConflictError(
                f"run {run_id} was modified concurrently; reload and retry"
            ) from exc
        return event

    @staticmethod
    def state_before_pause(session: Session, run: Run) -> RunState | None:
        """The state the run was in when it was last paused."""
        event = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.to_state == S.PAUSED.value)
            .order_by(RunEvent.seq.desc())
            .limit(1)
        )
        return RunState(event.from_state) if event and event.from_state else None
