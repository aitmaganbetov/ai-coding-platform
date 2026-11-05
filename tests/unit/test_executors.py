"""Executors: local restrictions and Docker sandbox hardening (ТЗ §12)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from ai_coding.config.models import DockerConfig, ResourceLimits
from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors import (
    CommandSpec,
    DockerSandboxExecutor,
    Executor,
    ExecutorError,
    LocalExecutor,
    Mount,
)

PY = Path(sys.executable).name
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
ALPINE = "alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"


def local(**kw: object) -> LocalExecutor:
    return LocalExecutor(allowed_executables={PY, "python", "python3"}, **kw)  # type: ignore[arg-type]


def py(code: str, cwd: Path, **kw: object) -> CommandSpec:
    return CommandSpec(argv=(sys.executable, "-c", code), cwd=cwd, **kw)  # type: ignore[arg-type]


def test_both_backends_implement_port() -> None:
    assert isinstance(local(), Executor)
    assert isinstance(DockerSandboxExecutor(), Executor)
    assert local().available()


@pytest.mark.parametrize(
    "kwargs",
    [{"argv": ()}, {"argv": ("",)}, {"argv": ("a\0b",)}, {"argv": ("x",), "timeout_seconds": 0}],
)
def test_command_spec_validation(tmp_path: Path, kwargs: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ExecutorError):
        CommandSpec(cwd=tmp_path, **kwargs)


async def test_local_runs_allowlisted_and_captures(tmp_path: Path) -> None:
    result = await local().run(
        py("import sys; print('out'); print('err', file=sys.stderr)", tmp_path)
    )
    assert result.ok and result.backend is ExecutionBackend.LOCAL
    assert result.stdout.strip() == "out" and result.stderr.strip() == "err"
    assert Path(result.argv[0]).is_absolute()


async def test_local_denies_unlisted_executable(tmp_path: Path) -> None:
    with pytest.raises(ExecutorError, match="not allowed"):
        await LocalExecutor(allowed_executables={"ruff"}).run(py("pass", tmp_path))
    with pytest.raises(ExecutorError, match="not found on PATH"):
        await LocalExecutor(allowed_executables={"no-such-tool-xyz"}).run(
            CommandSpec(argv=("no-such-tool-xyz",), cwd=tmp_path)
        )


async def test_local_env_is_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-0000000000000000000000")
    monkeypatch.setenv("LANG", "C.UTF-8")
    result = await local().run(
        py("import os, json; print(json.dumps(sorted(os.environ)))", tmp_path, env={"EXTRA": "1"})
    )
    names = result.stdout
    assert "OPENAI_API_KEY" not in names and "EXTRA" in names and "PATH" in names


async def test_local_output_is_bounded_and_redacted(tmp_path: Path) -> None:
    token = "ghp_" + "t" * 36
    result = await local().run(
        py(f"print('{token}'); print('x' * 100000)", tmp_path, max_output_bytes=1000)
    )
    assert token not in result.stdout
    assert result.stdout_truncated and len(result.stdout) <= 1100


@posix_only
async def test_local_timeout_kills_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child-alive"
    child = tmp_path / "child.py"
    child.write_text(
        f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n", encoding="utf-8"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = await local().run(
        CommandSpec(argv=(sys.executable, str(parent)), cwd=tmp_path, timeout_seconds=1.0)
    )
    assert result.timed_out and not result.ok
    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists(), "grandchild survived the timeout"


async def test_local_rejects_sandbox_only_options(tmp_path: Path) -> None:
    with pytest.raises(ExecutorError, match="sandbox options"):
        await local().run(py("pass", tmp_path, image=ALPINE))
    with pytest.raises(ExecutorError, match="network"):
        await local().run(py("pass", tmp_path, network=True))
    with pytest.raises(ExecutorError, match="does not exist"):
        await local().run(py("pass", tmp_path / "missing"))


async def test_local_timeout_capped_by_limits(tmp_path: Path) -> None:
    executor = local(limits=ResourceLimits(timeout_seconds=1))
    result = await executor.run(py("import time; time.sleep(10)", tmp_path, timeout_seconds=60))
    assert result.timed_out


@posix_only
async def test_local_core_dumps_disabled(tmp_path: Path) -> None:
    result = await local().run(
        py("import resource; print(resource.getrlimit(resource.RLIMIT_CORE))", tmp_path)
    )
    assert result.stdout.strip() == "(0, 0)"


# ------------------------------------------------------------------- docker --
def test_docker_argv_is_hardened(tmp_path: Path) -> None:
    executor = DockerSandboxExecutor(limits=ResourceLimits(cpus=1.5, memory_mb=512))
    spec = CommandSpec(
        argv=("gitleaks", "dir", "/src"),
        cwd=tmp_path,
        image=ALPINE,
        mounts=(Mount(tmp_path, "/src"),),
        workdir="/src",
        env={"FOO": "bar"},
    )
    argv = executor.build_argv(spec, "ai-coding-test")
    joined = " ".join(argv)
    for flag in (
        "--rm", "--network none", "--read-only", "--cap-drop ALL",
        "--security-opt no-new-privileges", "--user 1000:1000", "--pids-limit 256",
        "--cpus 1.5", "--memory 512m", "--pull never", "--workdir /src",
    ):  # fmt: skip
        assert flag in joined, flag
    assert f"type=bind,source={tmp_path.resolve()},target=/src,readonly" in argv
    assert "--privileged" not in argv and "docker.sock" not in joined
    assert argv[-5:] == ["--entrypoint", "gitleaks", ALPINE, "dir", "/src"]


@pytest.mark.parametrize(
    ("spec_kw", "message"),
    [
        ({"image": "alpine:3.20"}, "pinned by digest"),
        ({"image": ALPINE, "env": {"BAD-KEY": "x"}}, "invalid environment"),
        ({"image": ALPINE, "network": True}, "network is disabled"),
        ({"image": ALPINE, "mounts": (Mount(Path("/does/not/exist"), "/src"),)}, "does not exist"),
    ],
)
def test_docker_spec_rejections(tmp_path: Path, spec_kw: dict, message: str) -> None:  # type: ignore[type-arg]
    spec = CommandSpec(argv=("true",), cwd=tmp_path, **spec_kw)
    with pytest.raises(ExecutorError, match=message):
        DockerSandboxExecutor().build_argv(spec, "n")


def test_docker_socket_mount_forbidden(tmp_path: Path) -> None:
    sock = tmp_path / "docker.sock"
    sock.write_text("", encoding="utf-8")
    spec = CommandSpec(argv=("true",), cwd=tmp_path, image=ALPINE, mounts=(Mount(sock, "/s"),))
    with pytest.raises(ExecutorError, match="Docker socket"):
        DockerSandboxExecutor().build_argv(spec, "n")
    bad_target = CommandSpec(argv=("true",), cwd=tmp_path, image=ALPINE,
                             mounts=(Mount(tmp_path, "relative"),))  # fmt: skip
    with pytest.raises(ExecutorError, match="mount target"):
        DockerSandboxExecutor().build_argv(bad_target, "n")


def test_docker_network_when_allowed(tmp_path: Path) -> None:
    executor = DockerSandboxExecutor(docker_config=DockerConfig(network="bridge"))
    spec = CommandSpec(argv=("true",), cwd=tmp_path, image=ALPINE, network=True)
    assert "bridge" in executor.build_argv(spec, "n")
    unpinned = DockerSandboxExecutor(require_digest=False)
    assert "alpine:3.20" in unpinned.build_argv(
        CommandSpec(argv=("true",), cwd=tmp_path, image="alpine:3.20"), "n"
    )


def test_docker_unavailable_binary(tmp_path: Path) -> None:
    executor = DockerSandboxExecutor(docker="no-such-docker-binary")
    assert not executor.available()
    assert not executor.image_present(ALPINE)
