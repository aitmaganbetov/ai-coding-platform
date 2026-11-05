"""Docker sandbox: isolation properties verified inside a real container (ТЗ §12)."""

from __future__ import annotations

import asyncio
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


async def test_scratch_cleanup_removes_what_the_sandbox_user_protected(
    sandbox: DockerSandboxExecutor, tmp_path: Path
) -> None:
    """Go's module cache is read-only, npm/cargo create private directories; the cleanup
    script (run as the sandbox user) must remove them below a host-owned directory."""
    from ai_coding.tools.scratch import SCRATCH, clean_argv  # noqa: PLC0415

    deps = tmp_path / "deps"
    deps.mkdir()
    tmp_path.chmod(0o777)
    deps.chmod(0o777)
    make = (
        "mkdir -p /s/deps/mod/x@v1/sub /s/deps/cache/e2 /s/.hidden && "
        "touch /s/deps/mod/x@v1/sub/f.go /s/deps/cache/e2/blob /s/top && "
        "chmod -R a-w /s/deps/mod && chmod 700 /s/deps/cache /s/deps/cache/e2"
    )
    mount = (Mount(tmp_path, "/s", read_only=False),)
    assert (await sandbox.run(sh(make, tmp_path, mounts=mount))).exit_code == 0
    clean = CommandSpec(argv=clean_argv(), cwd=tmp_path, image=ALPINE,
                        mounts=(Mount(tmp_path, SCRATCH, read_only=False),))  # fmt: skip
    assert (await sandbox.run(clean)).exit_code == 0
    assert list(tmp_path.iterdir()) == []


def test_docker_client_env_reaches_remote_or_rootless_daemons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-807: what the docker CLI needs for a remote (TLS/SSH) or rootless daemon passes;
    the platform's own secrets do not."""
    from ai_coding.executors.docker import _docker_env  # noqa: PLC0415

    wanted = {"DOCKER_HOST": "ssh://ci@builder", "DOCKER_TLS_VERIFY": "1",
              "DOCKER_CERT_PATH": "/certs", "SSH_AUTH_SOCK": "/run/ssh.sock",
              "XDG_RUNTIME_DIR": "/run/user/1001", "DOCKER_CONTEXT": "rootless"}  # fmt: skip
    secrets = {"AWS_SECRET_ACCESS_KEY": "x", "GITHUB_TOKEN": "y",
               "AI_CODING_S3_SECRET_ACCESS_KEY": "z", "OPENAI_API_KEY": "w"}  # fmt: skip
    for key, value in {**wanted, **secrets}.items():
        monkeypatch.setenv(key, value)
    env = _docker_env()
    assert {k: env[k] for k in wanted} == wanted
    assert not set(secrets) & set(env)


def test_daemon_is_reached_through_docker_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor talks to the daemon DOCKER_HOST names (another socket path here)."""
    from ai_coding.executors import docker_available  # noqa: PLC0415

    require_images(ALPINE)
    socket = Path("/var/run/docker.sock")
    if not socket.exists():
        pytest.skip("the daemon does not listen on /var/run/docker.sock")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path / 'nowhere.sock'}")
    assert not docker_available()
    link = tmp_path / "daemon.sock"
    link.symlink_to(socket)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{link}")
    assert docker_available()
    sandbox = DockerSandboxExecutor()
    result = asyncio.run(sandbox.run(sh("echo via-docker-host", tmp_path)))
    assert result.ok and "via-docker-host" in result.stdout
