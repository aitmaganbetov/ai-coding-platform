"""Tests for stack detection (FR-02)."""

from __future__ import annotations

import json
from pathlib import Path

from ai_coding.detection import detect_stacks
from ai_coding.domain.enums import Stack


def stacks(root: Path) -> list[Stack]:
    return [e.stack for e in detect_stacks(root)]


def test_empty_directory(tmp_path: Path) -> None:
    assert detect_stacks(tmp_path) == []


def test_laravel_react_monorepo(tmp_path: Path) -> None:
    """AC-02 shape: Laravel backend + React/TS frontend in sub-directories."""
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^11.0"}}), encoding="utf-8"
    )
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^19"}, "devDependencies": {"typescript": "^5"}}),
        encoding="utf-8",
    )
    evidence = detect_stacks(tmp_path)
    assert [e.stack for e in evidence] == [
        Stack.LARAVEL, Stack.PHP, Stack.REACT, Stack.TYPESCRIPT, Stack.JAVASCRIPT,
    ]  # fmt: skip
    assert evidence[0].path == "backend/composer.json"


def test_laravel_by_artisan(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "artisan").write_text("", encoding="utf-8")
    assert stacks(tmp_path) == [Stack.LARAVEL, Stack.PHP]


def test_python_go_rust(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert stacks(tmp_path) == [Stack.PYTHON, Stack.GO, Stack.RUST]


def test_tsconfig_only(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert stacks(tmp_path) == [Stack.TYPESCRIPT]


def test_malformed_manifest_is_tolerated(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "composer.json").write_text("[1, 2]", encoding="utf-8")
    assert stacks(tmp_path) == [Stack.PHP, Stack.JAVASCRIPT]


def test_vendor_dirs_and_depth_are_skipped(tmp_path: Path) -> None:
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (tmp_path / "node_modules" / "go.mod").write_text("", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "go.mod").write_text("", encoding="utf-8")
    assert detect_stacks(tmp_path) == []
    assert stacks(tmp_path) == []
    assert [e.stack for e in detect_stacks(tmp_path, max_depth=2)] == [Stack.GO]
