"""Fixtures for tests that need Docker and the pinned tool images."""

from __future__ import annotations

import pytest

from ai_coding.executors import DockerSandboxExecutor, docker_available

ALPINE = "alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"


def require_images(*images: str) -> None:
    """Skip unless Docker runs and every image is present (no implicit pulls)."""
    if not docker_available():
        pytest.skip("Docker daemon not available")
    executor = DockerSandboxExecutor()
    missing = [i for i in images if not executor.image_present(i)]
    if missing:
        pytest.skip(
            f"image(s) not present locally: {', '.join(missing)} (run `ai-coding tools pull`)"
        )
