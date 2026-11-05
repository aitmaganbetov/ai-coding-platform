"""Platform cache for offline tool data (vulnerability databases; ADR-0027).

Location: ``$AI_CODING_CACHE_DIR`` or ``~/.cache/ai-coding``. Filled only by the
explicit, networked ``ai-coding tools pull``; audits then read it offline. It never
lives inside a scanned project, so audits stay read-only for the project (FR-06).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV = "AI_CODING_CACHE_DIR"


def cache_root() -> Path:
    """Root of the platform cache."""
    configured = os.environ.get(ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".cache" / "ai-coding"


def tool_cache(name: str) -> Path:
    """Cache directory of one tool (not created)."""
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"invalid cache name {name!r}")
    return cache_root() / name
