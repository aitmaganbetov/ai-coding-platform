"""Worker presence: workers announce themselves, idle or busy (B-703).

Otherwise a worker is only seen through the job leases it holds, and an idle worker (the
usual state of a healthy one) would look absent. Each worker writes a small JSON
heartbeat to ``<data dir>/workers/`` every :data:`BEAT_SECONDS` and removes it when it
stops; the API lists the heartbeats fresher than :data:`ALIVE`.

Files, not a table: the API and the workers already share the data directory (run
sources, uploads), and a heartbeat every few seconds would only churn the database. A
worker killed without cleaning up leaves its file behind: it stops counting after
:data:`ALIVE` and is deleted by the next worker that starts, once older than
:data:`FORGET`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

BEAT_SECONDS = 15.0
ALIVE = timedelta(seconds=60)  # four beats: a busy host or a slow disk is not a death
FORGET = timedelta(days=1)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def directory(data_dir: Path) -> Path:
    return data_dir / "workers"


def _path(data_dir: Path, owner: str) -> Path:
    return directory(data_dir) / f"{_UNSAFE.sub('_', owner)[:200]}.json"


def beat(data_dir: Path, owner: str, *, started: datetime, concurrency: int, active: int,
         now: datetime) -> None:  # fmt: skip
    """Write the worker's heartbeat (atomically: a reader never sees half a file)."""
    folder = directory(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = _path(data_dir, owner)
    record = {"id": owner, "host": socket.gethostname(), "pid": os.getpid(),
              "started": started.isoformat(), "seen": now.isoformat(),
              "concurrency": concurrency, "active": active}  # fmt: skip
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)


def leave(data_dir: Path, owner: str) -> None:
    """The worker stopped: it is no longer listed."""
    with contextlib.suppress(OSError):
        _path(data_dir, owner).unlink(missing_ok=True)


def forget_stale(data_dir: Path) -> int:
    """Delete heartbeats of workers gone for longer than :data:`FORGET`; how many."""
    removed = 0
    limit = time.time() - FORGET.total_seconds()
    with contextlib.suppress(OSError):
        for path in directory(data_dir).glob("*.json"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
    return removed


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def alive(data_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """The workers whose heartbeat is fresher than :data:`ALIVE`, by id; ``seen`` and
    ``started`` as datetimes. Unreadable or malformed files are skipped."""
    found: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for path in sorted(directory(data_dir).glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            seen = _time(record.get("seen"))
            if seen is None or now - seen > ALIVE:
                continue
            concurrency, active = record.get("concurrency"), record.get("active")
            found.append({
                "id": record["id"],
                "host": record.get("host") if isinstance(record.get("host"), str) else None,
                "started": _time(record.get("started")),
                "seen": min(seen, now),
                "concurrency": concurrency if isinstance(concurrency, int) else None,
                "active": active if isinstance(active, int) else 0,
            })  # fmt: skip
    return sorted(found, key=lambda w: str(w["id"]))


__all__ = ["ALIVE", "BEAT_SECONDS", "alive", "beat", "directory", "forget_stale", "leave"]
