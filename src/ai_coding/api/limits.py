"""Rate limiting (token bucket) and client address resolution (NFR-04).

The limiter is in-process: each API process enforces its own budget. Behind several
replicas the effective limit is multiplied by the replica count; a shared limiter
(Redis) is the documented extension point (ADR-0030).
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass, field


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
