"""Run leases (ADR-0006): one worker drives a run at a time.

A worker holds a run only while it renews its lease; a crashed worker's lease expires
and any other worker may take the run over and resume it (AC-04, NFR-03).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import or_, update

from ai_coding.observability import get_logger
from ai_coding.orchestrator.errors import RunLeaseError
from ai_coding.orchestrator.run_queries import get_run
from ai_coding.persistence.db import Database
from ai_coding.persistence.models import Run
from ai_coding.persistence.types import utcnow

_log = get_logger("ai_coding.orchestrator")


class LeaseKeeper:
    """Takes, renews and releases this worker's lease on a run."""

    def __init__(self, db: Database, worker_id: str, ttl: float) -> None:
        self.db = db
        self.worker_id = worker_id
        self.ttl = ttl

    def acquire(self, run_id: uuid.UUID, *, force: bool = False) -> None:
        """Take the lease if it is free, ours or expired (any lease with ``force``)."""
        now = utcnow()
        cond = [Run.id == run_id]
        if not force:
            cond.append(
                or_(
                    Run.lease_owner.is_(None),
                    Run.lease_owner == self.worker_id,
                    Run.lease_expires_at < now,
                )
            )
        with self.db.session() as session:
            result = session.execute(
                update(Run)
                .where(*cond)
                .values(
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.ttl),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                run = get_run(session, run_id)
                raise RunLeaseError(
                    f"run {run_id} is leased by {run.lease_owner} until "
                    f"{run.lease_expires_at.isoformat() if run.lease_expires_at else '?'}"
                )

    def renew(self, run_id: uuid.UUID) -> bool:
        """Extend our lease; ``False`` if another worker took the run."""
        with self.db.session() as session:
            result = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.lease_owner == self.worker_id)
                .values(lease_expires_at=utcnow() + timedelta(seconds=self.ttl))
                .execution_options(synchronize_session=False)
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    def release(self, run_id: uuid.UUID) -> None:
        """Give the run back (only if we still hold it)."""
        with self.db.session() as session:
            session.execute(
                update(Run)
                .where(Run.id == run_id, Run.lease_owner == self.worker_id)
                .values(lease_owner=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )

    async def heartbeat(self, run_id: uuid.UUID, on_lost: Callable[[], None]) -> None:
        """Renew every third of the TTL until cancelled; ``on_lost`` once it fails."""
        interval = max(self.ttl / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            if not await asyncio.to_thread(self.renew, run_id):
                on_lost()
                _log.warning("lease lost", run_id=str(run_id), worker=self.worker_id)
                return
