"""B-805: the sandbox on gVisor (``execution.docker.runtime: runsc``).

gVisor puts a user-space kernel between the project code and the host kernel. The CI
job "gVisor sandbox" installs runsc (pinned release, checksum-verified) and runs this
file with skips forbidden; elsewhere the runtime is usually absent.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ai_coding.config.models import DockerConfig, ResourceLimits
from ai_coding.executors import CommandSpec, DockerSandboxExecutor, ExecutorUnavailableError, Mount
from tests.integration.conftest import ALPINE, require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _runtimes() -> set[str]:
    out = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                         capture_output=True, text=True, check=False).stdout  # fmt: skip
    try:
        return set(json.loads(out or "{}"))
    except ValueError:
        return set()


def _run(runtime: str, script: str, tmp_path: Path) -> str:
    sandbox = DockerSandboxExecutor(docker_config=DockerConfig(runtime=runtime),
                                    limits=ResourceLimits(memory_mb=256, cpus=0.5))  # fmt: skip
    result = asyncio.run(sandbox.run(CommandSpec(
        argv=("sh", "-c", script), cwd=tmp_path, image=ALPINE,
        mounts=(Mount(tmp_path, "/src"),), timeout_seconds=120,
    )))  # fmt: skip
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_unknown_runtime_is_a_clear_error(tmp_path: Path) -> None:
    require_images(ALPINE)
    if "aicoding-missing-runtime" in _runtimes():
        pytest.skip("a runtime with this name exists")
    with pytest.raises(ExecutorUnavailableError, match="no runtime 'aicoding-missing-runtime'"):
        _run("aicoding-missing-runtime", "true", tmp_path)


def test_sandbox_on_gvisor(tmp_path: Path) -> None:
    require_images(ALPINE)
    if "runsc" not in _runtimes():
        pytest.skip("the Docker daemon has no 'runsc' runtime (gVisor)")
    tmp_path.chmod(0o755)
    (tmp_path / "code.txt").write_text("source\n", encoding="utf-8")
    (tmp_path / "code.txt").chmod(0o644)
    script = "; ".join([
        "dmesg 2>/dev/null | head -n 3 | tr '\\n' ' '; echo",  # gVisor prints its own boot
        "id -u",
        "grep -E '^CapEff:' /proc/self/status | tr -d '\\t '",
        "(touch /etc/x 2>/dev/null && echo ROOT-RW) || echo ROOT-RO",
        "(touch /src/new 2>/dev/null && echo SRC-RW) || echo SRC-RO",
        "cat /src/code.txt",
        "(mount -t tmpfs none /tmp 2>/dev/null && echo MOUNTED) || echo NO-MOUNT",
        "(wget -q -T 2 -O- http://1.1.1.1 >/dev/null 2>&1 && echo NET) || echo NO-NET",
        "ls /var/run/docker.sock 2>/dev/null | wc -l",
    ])  # fmt: skip
    lines = _run("runsc", script, tmp_path).splitlines()
    assert "gvisor" in lines[0].lower(), lines[0]  # the kernel is gVisor's, not the host's
    assert lines[1:] == ["1000", "CapEff:0000000000000000", "ROOT-RO", "SRC-RO", "source",
                         "NO-MOUNT", "NO-NET", "0"], lines  # fmt: skip
    assert not (tmp_path / "new").exists()
