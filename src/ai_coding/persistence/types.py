"""Portable column types: UUID and timezone-aware UTC timestamps on SQLite and PostgreSQL."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as UTC; always returns aware UTC datetimes.

    SQLite has no timezone support, so naive values read back are interpreted as UTC.
    Naive values are rejected on write to avoid silent local-time bugs.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; use datetime.now(UTC)")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):  # pragma: no cover - driver contract
            raise TypeError(f"unexpected datetime value {value!r}")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(UTC)


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 version 7): sortable primary keys (ADR-0005)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)
