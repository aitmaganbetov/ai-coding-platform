"""Tests for initial configuration generation."""

from __future__ import annotations

import pytest

from ai_coding.config.generator import (
    ConfigGenerationError,
    render_initial_config,
    sanitize_project_name,
)
from ai_coding.domain.enums import Mode, Stack


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("My Project", "My-Project"),
        ("..", "project"),
        ("ok_name-1.2", "ok_name-1.2"),
        ("", "project"),
    ],
)
def test_sanitize_project_name(raw: str, expected: str) -> None:
    assert sanitize_project_name(raw) == expected


def test_sanitize_truncates() -> None:
    assert len(sanitize_project_name("a" * 300)) == 100


@pytest.mark.parametrize("mode", list(Mode))
def test_render_is_valid_for_every_mode(mode: Mode) -> None:
    text, cfg = render_initial_config(name="demo", mode=mode, stacks=[Stack.PYTHON, Stack.GO])
    assert cfg.mode is mode
    assert cfg.stacks == [Stack.PYTHON, Stack.GO]
    assert "SECURITY: never put secret values" in text


def test_custom_default_branch_is_protected() -> None:
    _, cfg = render_initial_config(name="demo", default_branch="trunk")
    assert cfg.project.default_branch == "trunk"
    assert "trunk" in cfg.project.protected_branches


def test_name_with_quotes_is_escaped_then_rejected_by_schema() -> None:
    with pytest.raises(ConfigGenerationError, match=r"project\.name"):
        render_initial_config(name='bad"name')
