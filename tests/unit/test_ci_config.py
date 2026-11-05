"""CI must pull exactly the images the Docker tests need (no silent skips)."""

from __future__ import annotations

from pathlib import Path

from tests.integration.conftest import ALPINE

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_ci_pulls_the_sandbox_test_image() -> None:
    assert f"docker pull --quiet {ALPINE}" in CI.read_text(encoding="utf-8")
