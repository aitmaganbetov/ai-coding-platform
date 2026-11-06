"""Server work queue and worker pool (ADR-0013, B-703, B-704, NFR-02).

* :class:`JobQueue` — ``run_jobs`` rows leased with an expiry. On PostgreSQL the
  candidate row is locked with ``FOR UPDATE SKIP LOCKED`` so concurrent workers never
  take the same job; on SQLite a conditional ``UPDATE`` (compare-and-set) does the same.
* :class:`Worker` — runs up to ``concurrency`` jobs at once (default 10). A job
  executes or resumes its run through the orchestrator, which holds its own run
  lease and checkpoints; the worker heart-beats the job lease meanwhile.
* Recovery (AC-04): a worker that dies stops renewing; once the job lease expires
  another worker leases the job again and *resumes* the run from its last checkpoint
  (the run's own lease has expired too), without repeating confirmed effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from ai_coding.api.kinds import build_kind
from ai_coding.api.metrics import Metrics
from ai_coding.api.services import Server
from ai_coding.domain.enums import RunState
from ai_coding.observability import get_logger
from ai_coding.orchestrator.states import TERMINAL_STATES
from ai_coding.persistence.models import Project, Run, RunJob
from ai_coding.persistence.types import utcnow

_log = get_logger("ai_coding.worker")
MAX_ATTEMPTS = 3
PURGE_INTERVAL = 3600.0
_ERROR_CHARS = 2000


@dataclass(frozen=True, slots=True)
class LeasedJob:
    """A job this worker owns until ``lease`` expires."""

    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    attempts: int


class JobQueue:
    """Lease, renew and finish ``run_jobs``."""

    def __init__(self, server: Server, owner: str, *, lease_seconds: float = 60.0) -> None:
        self.server = server
        self.owner = owner
        self.lease = timedelta(seconds=lease_seconds)

    def lease_next(self) -> LeasedJob | None:
        """The oldest available job (queued, or leased by a worker that stopped renewing)."""
        now = utcnow()
        available = or_(
            and_(RunJob.state == "queued", RunJob.available_at <= now),
            and_(RunJob.state == "leased", RunJob.lease_expires_at < now),
        )
        query = select(RunJob).where(available).order_by(RunJob.created_at).limit(1)
        if not self.server.db.is_sqlite:
            query = query.with_for_update(skip_locked=True)
        with self.server.session() as s:
            job = s.scalar(query)
            if job is None:
                return None
            if job.attempts >= MAX_ATTEMPTS:
                job.state, job.finished_at = "failed", now
                job.error = (job.error or "") + " | gave up after repeated lease expiry"
                return None
            claimed = s.execute(
                update(RunJob)
                .where(
                    RunJob.id == job.id, RunJob.state == job.state, RunJob.attempts == job.attempts
                )
                .values(
                    state="leased",
                    lease_owner=self.owner,
                    lease_expires_at=now + self.lease,
                    attempts=job.attempts + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return None  # another worker won (SQLite compare-and-set)
            return LeasedJob(job.id, job.run_id, job.kind, job.attempts + 1)

    def renew(self, job_id: uuid.UUID) -> bool:
        with self.server.session() as s:
            result = s.execute(
                update(RunJob)
                .where(
                    RunJob.id == job_id, RunJob.lease_owner == self.owner, RunJob.state == "leased"
                )
                .values(lease_expires_at=utcnow() + self.lease)
                .execution_options(synchronize_session=False)
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    def finish(self, job_id: uuid.UUID, *, ok: bool, error: str | None = None) -> None:
        with self.server.session() as s:
            s.execute(
                update(RunJob)
                .where(RunJob.id == job_id, RunJob.lease_owner == self.owner)
                .values(
                    state="done" if ok else "failed",
                    finished_at=utcnow(),
                    error=(error or None) and error[:_ERROR_CHARS],
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )


class Worker:
    """Executes queued runs, ``concurrency`` at a time."""

    def __init__(
        self,
        server: Server,
        *,
        concurrency: int | None = None,
        poll_seconds: float = 1.0,
        lease_seconds: float = 60.0,
        metrics: Metrics | None = None,
        owner: str | None = None,
    ) -> None:
        self.server = server
        self.concurrency = concurrency or server.settings.workers
        self.poll = poll_seconds
        self.lease_seconds = lease_seconds
        self.owner = owner or f"{server.worker_id}:{uuid.uuid4().hex[:8]}"
        self.queue = JobQueue(server, self.owner, lease_seconds=lease_seconds)
        self.metrics = metrics or Metrics()
        self._slots = asyncio.Semaphore(self.concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        # One thread per running job; the default pool (min(32, CPUs + 4)) would cap
        # the concurrency on small hosts below NFR-02.
        self._pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="ai-coding-job")

    async def run(self, stop: asyncio.Event) -> None:
        """Lease and execute jobs until ``stop`` is set; then wait for running jobs.

        With ``AI_CODING_SERVER_RETENTION_DAYS`` set, retention is applied hourly.
        """
        next_purge = 0.0
        while not stop.is_set():
            loop_time = asyncio.get_running_loop().time()
            if self.server.settings.retention_days and loop_time >= next_purge:
                next_purge = loop_time + PURGE_INTERVAL
                await self._purge()
            started = await self._fill()
            if not started:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll)
        await self.drain()

    async def run_until_idle(self, timeout: float = 60.0) -> int:
        """Process jobs until the queue is empty and nothing runs (tests, one-shot use)."""
        done = 0
        async with asyncio.timeout(timeout):
            while True:
                done += await self._fill()
                if not self._tasks:
                    if await self._fill() == 0:
                        return done
                    done += 1
                    continue
                await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)

    async def _purge(self) -> None:
        from ai_coding.api.lifecycle import purge_expired  # noqa: PLC0415

        try:
            counts = await asyncio.to_thread(purge_expired, self.server)
        except Exception as exc:
            _log.warning("retention purge failed", error=str(exc))
            return
        _log.info("retention purge", **counts)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _fill(self) -> int:
        """Start as many jobs as free slots allow; the number started."""
        started = 0
        while not self._slots.locked():
            job = await asyncio.to_thread(self.queue.lease_next)
            if job is None:
                break
            await self._slots.acquire()
            task = asyncio.create_task(self._process(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started += 1
        return started

    async def _process(self, job: LeasedJob) -> None:
        self.metrics.active_runs += 1
        beat = asyncio.create_task(self._heartbeat(job))
        try:
            await self._execute(job)
            await asyncio.to_thread(self.queue.finish, job.id, ok=True)
            self.metrics.jobs_done += 1
        except Exception as exc:
            _log.warning("job failed", job=str(job.id), run_id=str(job.run_id), error=str(exc))
            await asyncio.to_thread(self.queue.finish, job.id, ok=False, error=repr(exc))
            self.metrics.jobs_failed += 1
        finally:
            beat.cancel()
            self.metrics.active_runs -= 1
            self._slots.release()

    async def _heartbeat(self, job: LeasedJob) -> None:
        while True:
            await asyncio.sleep(max(self.lease_seconds / 3, 0.05))
            if not await asyncio.to_thread(self.queue.renew, job.id):
                return

    async def _execute(self, job: LeasedJob) -> None:
        """Run the job in its own thread with its own event loop.

        Handlers do synchronous work (database, git, files). In the worker's loop that
        would stall the other runs and, with ``serve --with-worker``, the API itself.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, asyncio.run, self._execute_isolated(job))

    async def _execute_isolated(self, job: LeasedJob) -> None:
        with self.server.session() as s:
            run = s.get(Run, job.run_id)
            if run is None:
                return
            project = s.get(Project, run.project_id)
            if project is None:
                return
            s.expunge(project)
            state = RunState(run.state)
            kind, params = run.options.get("kind"), run.options.get("params") or {}
            source = run.options.get("source")
            actor = run.requested_by
        if state in TERMINAL_STATES:
            return
        if not kind:
            raise RuntimeError(f"run {job.run_id} has no server run kind")
        orch, config = self.server.orchestrator(project, actor=actor)
        if source:  # a workspace snapshot (ADR-0038): checked again, it is a path
            orch.source_root = self.server.run_source(project.id, str(source))
        root = orch.source_root or orch.project_root
        (registry, _steps), _ = build_kind(str(kind), root, config, params)
        orch.registry = registry
        orch.lease_ttl = self.lease_seconds
        if state is RunState.CREATED and job.kind == "execute":
            status = await orch.execute(job.run_id)
        else:  # crash recovery, approval or pause: continue from the last checkpoint
            status = await orch.resume(job.run_id)
        _log.info("job finished", job=str(job.id), run_id=str(job.run_id), state=status.state.value)
