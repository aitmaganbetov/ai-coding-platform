"""Detect technology stacks from marker files.

Detection is deterministic and only *reads* manifest files (never executes
anything from the repository). It inspects the project root and, to support
monorepos such as Laravel + React, first-level sub-directories. Users can always
override the result via ``stacks:`` in ``.ai-coding.yml`` (FR-02).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import Stack

_SKIP_DIRS = frozenset(
    {".git", "node_modules", "vendor", ".venv", "venv", "target", "dist", "build", "__pycache__"}
)
_MAX_MANIFEST_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class StackEvidence:
    """Why a stack was detected."""

    stack: Stack
    path: str  # relative POSIX path of the marker file


_SOURCE_EXTENSIONS: dict[str, Stack] = {
    ".py": Stack.PYTHON,
    ".php": Stack.PHP,
    ".go": Stack.GO,
    ".rs": Stack.RUST,
    ".ts": Stack.TYPESCRIPT,
    ".tsx": Stack.TYPESCRIPT,
    ".js": Stack.JAVASCRIPT,
    ".jsx": Stack.JAVASCRIPT,
}


def detect_stacks(root: Path, *, max_depth: int = 1) -> list[StackEvidence]:
    """Return detected stacks with evidence, ordered by :class:`Stack` declaration order.

    Manifests are authoritative. Source files (by extension, same depth) are a
    fallback for projects without a manifest, e.g. a scripts folder.
    """
    found: dict[Stack, str] = {}
    dirs = _candidate_dirs(root, max_depth)
    for directory in dirs:
        for stack, marker in _detect_in(directory):
            found.setdefault(stack, (marker.relative_to(root)).as_posix())
    for directory in dirs:
        try:
            files = sorted(p for p in directory.iterdir() if p.is_file() and not p.is_symlink())
        except OSError:
            continue
        for path in files:
            by_ext = _SOURCE_EXTENSIONS.get(path.suffix.lower())
            if by_ext is not None:
                found.setdefault(by_ext, path.relative_to(root).as_posix())
    order = list(Stack)
    return [StackEvidence(s, found[s]) for s in sorted(found, key=order.index)]


def _candidate_dirs(root: Path, max_depth: int) -> list[Path]:
    dirs = [root]
    frontier = [root]
    for _ in range(max_depth):
        nxt: list[Path] = []
        for d in frontier:
            try:
                children = sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink())
            except OSError:
                continue
            nxt.extend(
                c for c in children if c.name not in _SKIP_DIRS and not c.name.startswith(".")
            )
        dirs.extend(nxt)
        frontier = nxt
    return dirs


def _detect_in(d: Path) -> list[tuple[Stack, Path]]:
    hits: list[tuple[Stack, Path]] = []

    composer = d / "composer.json"
    if composer.is_file():
        hits.append((Stack.PHP, composer))
        data = _read_json(composer)
        requires = {**_dict(data.get("require")), **_dict(data.get("require-dev"))}
        if "laravel/framework" in requires or (d / "artisan").is_file():
            hits.append((Stack.LARAVEL, composer))

    package = d / "package.json"
    if package.is_file():
        data = _read_json(package)
        deps = {
            **_dict(data.get("dependencies")),
            **_dict(data.get("devDependencies")),
            **_dict(data.get("peerDependencies")),
        }
        hits.append((Stack.JAVASCRIPT, package))
        if "react" in deps:
            hits.append((Stack.REACT, package))
        if "typescript" in deps:
            hits.append((Stack.TYPESCRIPT, package))
    tsconfig = d / "tsconfig.json"
    if tsconfig.is_file():
        hits.append((Stack.TYPESCRIPT, tsconfig))

    for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"):
        marker = d / name
        if marker.is_file():
            hits.append((Stack.PYTHON, marker))
            break

    go_mod = d / "go.mod"
    if go_mod.is_file():
        hits.append((Stack.GO, go_mod))

    cargo = d / "Cargo.toml"
    if cargo.is_file():
        hits.append((Stack.RUST, cargo))
    return hits


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
