"""Tool-level suppressions found in the project (B-225).

Scanners have their own ignore mechanisms — ``.semgrepignore``, ``.gitleaksignore``,
``.trivyignore``, inline ``nosemgrep`` / ``gitleaks:allow`` comments. They bypass the
platform baseline (no owner, reason or expiry; ТЗ §10), so the audit report lists
them for the reviewer. Nothing is changed; the scan is read-only and bounded.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IGNORE_FILES: dict[str, str] = {
    ".semgrepignore": "semgrep",
    ".gitleaksignore": "gitleaks",
    ".gitleaks.toml": "gitleaks",
    ".trivyignore": "trivy",
    ".trivyignore.yaml": "trivy",
    "trivy.yaml": "trivy",
    "osv-scanner.toml": "osv-scanner",
    ".eslintignore": "eslint",
    "phpstan-baseline.neon": "phpstan",
}
INLINE = re.compile(
    r"nosemgrep|gitleaks:allow|trivy:ignore|eslint-disable|@phpstan-ignore|phpstan-ignore-"
)
_INLINE_TOOL = {"nosemgrep": "semgrep", "gitleaks:allow": "gitleaks", "trivy:ignore": "trivy",
                "eslint-disable": "eslint", "@phpstan-ignore": "phpstan",
                "phpstan-ignore-": "phpstan"}  # fmt: skip
_SKIP_DIRS = frozenset({".git", ".ai-coding", "node_modules", "vendor", ".venv", "venv",
                        "target", "dist", "build", "__pycache__"})  # fmt: skip
_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".php",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".yml",
        ".yaml",
        ".toml",
        ".json",
        ".env",
        ".ini",
        ".cfg",
        ".conf",
        ".tf",
        ".sh",
        ".vue",
        ".blade",
    }
)
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_LOCATIONS = 50
SEMGREP_DEFAULT_NOTE = (
    "no .semgrepignore: Semgrep applies its default ignore list (tests/, test/, "
    "node_modules/, vendor/, build/, dist/ …), so those paths are not scanned"
)


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str
    tool: str
    entries: int


def _count_entries(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))


def scan_suppressions(root: Path) -> dict[str, Any]:
    """Ignore files and inline suppression markers under ``root``."""
    root = root.resolve()
    files: list[_Entry] = []
    inline: dict[str, int] = {}
    locations: list[str] = []
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        base = Path(dirpath)
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if name in IGNORE_FILES:
                files.append(_Entry(rel, IGNORE_FILES[name], _count_entries(path)))
            if path.suffix not in _TEXT_SUFFIXES or path.is_symlink():
                continue
            scanned += 1
            if scanned > MAX_FILES:
                truncated = True
                break
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for match in INLINE.finditer(line):
                    tool = _INLINE_TOOL[match.group(0)]
                    inline[tool] = inline.get(tool, 0) + 1
                    if len(locations) < MAX_LOCATIONS:
                        locations.append(f"{rel}:{lineno} ({match.group(0)})")
        if truncated:
            break
    notes = (
        []
        if any(f.tool == "semgrep" and f.path == ".semgrepignore" for f in files)
        else [SEMGREP_DEFAULT_NOTE]
    )
    return {
        "files": [{"path": f.path, "tool": f.tool, "entries": f.entries} for f in files],
        "inline": dict(sorted(inline.items())),
        "locations": locations,
        "notes": notes + (["inline scan stopped after the file limit"] if truncated else []),
    }
