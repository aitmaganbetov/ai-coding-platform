"""Docker sandbox: isolation properties verified inside a real container (ТЗ §12)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_coding.config.models import ResourceLimits
from ai_coding.executors import CommandSpec, DockerSandboxExecutor, ExecutorUnavailableError, Mount
from tests.integration.conftest import ALPINE, require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
def sandbox() -> DockerSandboxExecutor:
    require_images(ALPINE)
    return DockerSandboxExecutor(limits=ResourceLimits(memory_mb=256, cpus=0.5))


def sh(script: str, tmp_path: Path, **kw: object) -> CommandSpec:
    return CommandSpec(argv=("sh", "-c", script), cwd=tmp_path, image=ALPINE, **kw)  # type: ignore[arg-type]


async def test_isolation_properties(sandbox: DockerSandboxExecutor, tmp_path: Path) -> None:
    (tmp_path / "code.txt").write_text("source\n", encoding="utf-8")
    tmp_path.chmod(0o755)
    (tmp_path / "code.txt").chmod(0o644)
    script = (
        "id -u; "
        "(wget -q -T 2 -O- http://example.com >/dev/null 2>&1 && echo NET) || echo NO-NET; "
        "(touch /root-write 2>/dev/null && echo ROOT-RW) || echo ROOT-RO; "
        "(touch /src/new 2>/dev/null && echo SRC-RW) || echo SRC-RO; "
        "cat /src/code.txt; "
        "(touch /tmp/scratch && echo TMP-RW); "
        "grep -c docker.sock /proc/mounts || true; "
        "grep CapEff /proc/self/status"
    )
    result = await sandbox.run(sh(script, tmp_path, mounts=(Mount(tmp_path, "/src"),)))
    lines = result.stdout.split()
    assert result.ok, result.stderr
    assert lines[0] == "1000"
    for token in ("NO-NET", "ROOT-RO", "SRC-RO", "source", "TMP-RW"):
        assert token in lines, token
    assert "CapEff:\t0000000000000000" in result.stdout
    assert not (tmp_path / "new").exists()


async def test_timeout_kills_container(sandbox: DockerSandboxExecutor, tmp_path: Path) -> None:
    started = time.monotonic()
    result = await sandbox.run(sh("sleep 60", tmp_path, timeout_seconds=2))
    assert result.timed_out and time.monotonic() - started < 30


async def test_missing_image_is_not_pulled(sandbox: DockerSandboxExecutor, tmp_path: Path) -> None:
    ghost = "alpine@sha256:" + "0" * 64
    with pytest.raises(ExecutorUnavailableError, match="not present locally"):
        await sandbox.run(CommandSpec(argv=("true",), cwd=tmp_path, image=ghost))
    assert not sandbox.image_present(ghost)
