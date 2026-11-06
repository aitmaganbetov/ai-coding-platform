"""Rate limiting (token bucket) and client address resolution (NFR-04).

:class:`RateLimiter` is in-process: each API process enforces its own budget, so with
several processes the effective limit is multiplied. :class:`SharedRateLimiter` keeps
the counters in the database, shared by every API process and replica (S9, B-807).
"""

from __future__ import annotations

import hashlib
import ipaddress
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_coding.observability import get_logger
from ai_coding.persistence import Database
from ai_coding.persistence.models import RateLimitCounter

_log = get_logger(__name__)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """``per_minute`` requests per key, with bursts up to the same amount."""

    per_minute: int
    max_keys: int = 50_000
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key: str, now: float | None = None) -> float | None:
        """``None`` if allowed, else the seconds to wait (Retry-After)."""
        now = time.monotonic() if now is None else now
        rate = self.per_minute / 60.0
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    self._evict(now)
                bucket = self._buckets[key] = _Bucket(float(self.per_minute), now)
            bucket.tokens = min(
                float(self.per_minute), bucket.tokens + (now - bucket.updated) * rate
            )
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None
            return (1.0 - bucket.tokens) / rate

    def _evict(self, now: float) -> None:
        """Drop buckets that are full again (idle keys) to bound memory."""
        full = [
            k
            for k, b in self._buckets.items()
            if b.tokens + (now - b.updated) * self.per_minute / 60.0 >= self.per_minute
        ]
        for key in full or list(self._buckets)[: len(self._buckets) // 2]:
            del self._buckets[key]


@dataclass(slots=True)
class _Shared:
    minute: int
    previous: int = 0  # all processes, the minute before
    current: int = 0  # all processes, this minute, as of the last sync
    pending: int = 0  # this process, not yet sent to the database
    in_flight: int = 0  # this process, being sent right now
    synced_at: float = float("-inf")


class SharedRateLimiter:
    """``per_minute`` requests per key across all API processes (sliding-window estimate).

    Each process counts locally and adds its count to the key's row for the current
    minute at most every ``sync_seconds`` (``INSERT … ON CONFLICT DO UPDATE …
    RETURNING``), reading back the total of every process. A database write per request
    made all requests of one client queue on one row lock (load test: p95 0.4 s →
    1.9 s); batching bounds the writes to 1/``sync_seconds`` per key and process, at
    the price of at most ``rate × sync_seconds`` extra requests per process. The
    database round trip happens outside the process lock, so other requests never
    wait for it.

    The estimate weights the previous minute by the part still inside the window, so
    a burst at a minute boundary cannot double the limit. If the database fails, this
    process's own count decides (a failure never resets it), and the database is left
    alone for a back-off period (1 s doubling to 30 s): only one request at a time probes
    it, the others never wait for an unreachable database.

    At ``max_keys`` keys, room is made without losing counts: keys of minutes outside
    the window go first, then keys whose counts are all in the database (a later sync
    reads them back); only if every key still holds unsent counts are those sent in one
    transaction before the keys are dropped.
    """

    BACKOFF_MIN = 1.0
    BACKOFF_MAX = 30.0

    def __init__(
        self, db: Database, per_minute: int, *, sync_seconds: float = 0.5,
        purge_every: int = 1000, max_keys: int = 50_000,
    ) -> None:  # fmt: skip
        self.db = db
        self.per_minute = per_minute
        self.sync_seconds = sync_seconds
        self.purge_every = purge_every
        self.max_keys = max_keys
        self._state: dict[str, _Shared] = {}
        self._syncs = 0
        self._lock = threading.Lock()
        self._down_until = float("-inf")  # no database calls before this time
        self._backoff = 0.0
        self._probing = False  # one request at a time tries a database that failed

    def check(self, key: str, now: float | None = None) -> float | None:
        """``None`` if allowed, else the seconds to wait (Retry-After)."""
        now = time.time() if now is None else now
        minute, into = int(now // 60), (now % 60) / 60.0
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        flush: list[tuple[str, int, int]] = []  # (key, minute, count) not yet in the database
        with self._lock:
            state = self._state.get(digest)
            if state is None or state.minute != minute:
                if state is not None and state.pending:
                    flush.append((digest, state.minute, state.pending))
                if state is None and len(self._state) >= self.max_keys:
                    flush += self._make_room(minute)
                old, state = state, _Shared(minute)
                self._state[digest] = state
                if old is not None and old.minute == minute - 1:
                    state.previous = old.current + old.pending + old.in_flight
        if flush:
            self._send_many(flush, now)
        if self.sync_seconds <= 0 or now - state.synced_at >= self.sync_seconds:
            self._sync(digest, state, now)
        with self._lock:
            estimate = (state.previous * (1.0 - into) + state.current + state.pending
                        + state.in_flight)  # fmt: skip
            if estimate + 1 > self.per_minute:
                return max(1.0, 60.0 * (1.0 - into))
            state.pending += 1
        if self.sync_seconds <= 0:
            self._sync(digest, state, now)
        return None

    def _make_room(self, minute: int) -> list[tuple[str, int, int]]:
        """Free key slots (under the lock); the counts that must still be sent."""
        for digest in [d for d, st in self._state.items() if st.minute < minute - 1]:
            del self._state[digest]  # outside the sliding window: no longer counts
        if len(self._state) >= self.max_keys:
            for digest in [d for d, st in self._state.items()
                           if not st.pending and not st.in_flight]:  # fmt: skip
                del self._state[digest]  # all in the database; a sync reads it back
        unsent: list[tuple[str, int, int]] = []
        if len(self._state) >= self.max_keys:
            for digest, st in list(self._state.items()):
                if not st.in_flight:  # in flight: another thread is sending it now
                    unsent.append((digest, st.minute, st.pending))
                    del self._state[digest]
        return unsent

    def _database_usable(self, now: float) -> bool:
        """False during the back-off after a failure (one caller may probe after it)."""
        with self._lock:
            if now >= self._down_until and not self._probing:
                self._probing = self._backoff > 0
                return True
            return False

    def _record(self, ok: bool, now: float) -> None:
        with self._lock:
            self._probing = False
            if ok:
                self._backoff, self._down_until = 0.0, float("-inf")
            else:
                self._backoff = min(self.BACKOFF_MAX, max(self.BACKOFF_MIN, self._backoff * 2))
                self._down_until = now + self._backoff

    def _sync(self, digest: str, state: _Shared, now: float) -> None:
        """Send this process's pending count; read every process's totals."""
        with self._lock:
            if state.in_flight:  # another thread is syncing this key right now
                return
            if now < self._down_until or self._probing:
                return  # back-off: keep counting locally, do not wait for the database
            self._probing = self._backoff > 0
            state.in_flight, state.pending = state.pending, 0
            state.synced_at = now
            sending = state.in_flight
        totals = self._send(digest, state.minute, sending)
        self._record(totals is not None, now)
        with self._lock:
            state.in_flight = 0
            if totals is None:  # database unavailable: keep counting locally
                state.pending += sending
            else:
                state.current, previous = totals
                state.previous = previous or state.previous

    def _send_many(self, counts: list[tuple[str, int, int]], now: float) -> None:
        """Add counts of keys leaving this process's state, in one transaction."""
        counts = [c for c in counts if c[2]]
        if not counts or not self._database_usable(now):
            return
        try:
            with self.db.session() as s:
                for digest, minute, count in counts:
                    self._increment(s, digest, minute, count)
        except SQLAlchemyError as exc:
            _log.warning("shared rate limiter unavailable; counts not shared", error=str(exc))
            self._record(False, now)
            return
        self._record(True, now)

    def _send(self, digest: str, minute: int, count: int) -> tuple[int, int] | None:
        """Add ``count``; ``(current, previous)`` totals of all processes, or ``None``."""
        try:
            with self.db.session() as s:
                current = self._increment(s, digest, minute, count)
                previous = s.scalar(
                    select(RateLimitCounter.count).where(
                        RateLimitCounter.key == digest, RateLimitCounter.minute == minute - 1
                    )
                )
                with self._lock:
                    self._syncs += 1
                    purge = self._syncs % self.purge_every == 0
                if purge:
                    s.execute(delete(RateLimitCounter).where(RateLimitCounter.minute < minute - 1))
        except SQLAlchemyError as exc:
            _log.warning("shared rate limiter unavailable; counting locally", error=str(exc))
            return None
        return current, previous or 0

    @staticmethod
    def _increment(s: Session, digest: str, minute: int, by: int) -> int:
        dialect = s.get_bind().dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        statement = (
            insert(RateLimitCounter)
            .values(key=digest, minute=minute, count=by)
            .on_conflict_do_update(
                index_elements=["key", "minute"], set_={"count": RateLimitCounter.count + by}
            )
            .returning(RateLimitCounter.count)
        )
        return int(s.execute(statement).scalar_one())


def client_address(
    peer: str | None, forwarded_for: str | None, trusted_proxies: frozenset[str]
) -> str:
    """Client IP: ``X-Forwarded-For`` is honoured only from a trusted proxy.

    The right-most address not belonging to a trusted proxy is the client; a spoofed
    left-most entry therefore cannot change the rate-limit key.
    """
    peer = peer or "unknown"
    if not forwarded_for or peer not in trusted_proxies:
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if hop not in trusted_proxies:
            return hop
    return peer
