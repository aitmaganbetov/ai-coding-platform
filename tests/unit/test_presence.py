"""Worker presence: heartbeat files under the data directory (ai_coding.api.presence)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ai_coding.api import presence
from ai_coding.persistence.types import utcnow


def test_a_heartbeat_lists_the_worker_until_it_is_too_old(tmp_path: Path) -> None:
    now = utcnow()
    presence.beat(tmp_path, "host:12:ab/../x", started=now - timedelta(hours=2), concurrency=4,
                  active=1, now=now)  # fmt: skip
    # The owner never escapes the directory, and no temporary file is left behind.
    assert [p.name for p in presence.directory(tmp_path).iterdir()] == ["host_12_ab_.._x.json"]
    (worker,) = presence.alive(tmp_path, now + timedelta(seconds=30))
    assert worker["id"] == "host:12:ab/../x" and worker["active"] == 1
    assert worker["concurrency"] == 4 and worker["host"]
    assert worker["started"] == now - timedelta(hours=2) and worker["seen"] == now
    assert presence.alive(tmp_path, now + presence.ALIVE + timedelta(seconds=1)) == []
    presence.leave(tmp_path, "host:12:ab/../x")
    presence.leave(tmp_path, "host:12:ab/../x")  # already gone: fine
    assert presence.alive(tmp_path, now) == []


def test_malformed_heartbeats_are_skipped(tmp_path: Path) -> None:
    now = utcnow()
    assert presence.alive(tmp_path, now) == []  # no directory yet
    folder = presence.directory(tmp_path)
    folder.mkdir()
    (folder / "garbage.json").write_text("{not json", encoding="utf-8")
    (folder / "list.json").write_text("[1, 2]", encoding="utf-8")
    (folder / "naive.json").write_text(json.dumps({"id": "n", "seen": "2026-11-06T10:00:00"}),
                                       encoding="utf-8")  # fmt: skip
    (folder / "odd.json").write_text(json.dumps({"id": "o", "seen": now.isoformat(), "host": 5,
                                                 "concurrency": "many", "started": "never"}),
                                     encoding="utf-8")  # fmt: skip
    (odd,) = presence.alive(tmp_path, now)
    assert odd == {"id": "o", "host": None, "started": None, "seen": now, "concurrency": None,
                   "active": 0}  # fmt: skip
