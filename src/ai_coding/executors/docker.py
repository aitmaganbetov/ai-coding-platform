"""Docker Sandbox Executor (ТЗ §12; ADR-0007, ADR-0018).

Every container runs with: no network (unless policy grants it), read-only root
filesystem, all capabilities dropped, ``no-new-privileges``, a non-root user,
pids/CPU/memory limits, a size-limited ``/tmp`` tmpfs and read-only bind mounts.
The Docker socket is never mounted and ``--privileged`` is never used. Images must
be pinned by digest (NFR-05).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from ai_coding.config.models import DockerConfig, ResourceLimits
from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors.base import (
    CommandSpec,
    ExecutionResult,
    ExecutorError,
    ExecutorUnavailableError,
    Mount,
)
from ai_coding.executors.egress import PROXY_ENV
from ai_coding.executors.egress import egress_gateway as _gateway
from ai_coding.executors.process import run_process
from ai_coding.security import redact

_DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_FORBIDDEN_MOUNT_SOURCES = ("/var/run/docker.sock", "/run/docker.sock")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def docker_available(docker: str = "docker") -> bool:
    """Whether a Docker daemon answers."""
    exe = shutil.which(docker)
    if exe is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [exe, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=15, check=False, env=_docker_env(),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


class DockerSandboxExecutor:
    """Runs commands inside a locked-down, throw-away container."""

    backend = ExecutionBackend.DOCKER

    def __init__(
        self,
        *,
        limits: ResourceLimits | None = None,
        docker_config: DockerConfig | None = None,
        docker: str = "docker",
        require_digest: bool = True,
    ) -> None:
        self._limits = limits or ResourceLimits()
        self._cfg = docker_config or DockerConfig()
        self._docker = docker
        self._require_digest = require_digest

    def available(self) -> bool:
        """Whether the Docker daemon is reachable."""
        return docker_available(self._docker)

    def build_argv(
        self, spec: CommandSpec, name: str, *, egress: tuple[str, str] | None = None
    ) -> list[str]:
        """``docker run`` argv for ``spec`` (exposed for review and tests).

        ``egress`` is ``(internal network, proxy URL)`` for a step limited to an
        allowlist: the container gets no route outside, only the proxy.
        """
        image = spec.image or self._cfg.image
        if self._require_digest and not _DIGEST_REF.match(image):
            raise ExecutorError(f"image '{image}' must be pinned by digest (name@sha256:…)")
        network = "bridge" if spec.network else "none"
        if spec.network and self._cfg.network == "none":
            raise ExecutorError("sandbox network is disabled by execution.docker.network")
        if egress is not None:
            network = egress[0]
        argv = [
            "run", "--rm", "--name", name,
            "--pull", "never",  # images come from `ai-coding tools pull`: no hidden egress
            "--network", network,
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self._cfg.user,
            "--pids-limit", "256",
            "--cpus", str(self._limits.cpus),
            "--memory", f"{self._limits.memory_mb}m",
            "--memory-swap", f"{self._limits.memory_mb}m",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=512m",  # noqa: S108 - container path
            "--env", "HOME=/tmp",
            "--env", "XDG_CACHE_HOME=/tmp/.cache",
        ]  # fmt: skip
        for key, value in spec.env.items():
            if not _ENV_KEY.match(key):
                raise ExecutorError(f"invalid environment variable name {key!r}")
            argv += ["--env", f"{key}={value}"]
        if egress is not None:
            argv += ["--dns", "0.0.0.0"]  # noqa: S104 - no resolver: only the proxy resolves names
            for key in PROXY_ENV:
                argv += ["--env", f"{key}={egress[1]}"]
            argv += [
                "--env",
                "NO_PROXY=localhost,127.0.0.1",
                "--env",
                "no_proxy=localhost,127.0.0.1",
            ]
        for mount in spec.mounts:
            argv += ["--mount", self._mount_arg(mount)]
        if spec.workdir:
            argv += ["--workdir", spec.workdir]
        argv += ["--entrypoint", spec.argv[0], image, *spec.argv[1:]]
        return argv

    @staticmethod
    def _mount_arg(mount: Mount) -> str:
        source = mount.source.resolve()
        if str(source) in _FORBIDDEN_MOUNT_SOURCES or source.name == "docker.sock":
            raise ExecutorError("mounting the Docker socket is forbidden (ТЗ §12)")
        if not source.exists():
            raise ExecutorError(f"mount source {source} does not exist")
        if not mount.target.startswith("/") or "," in mount.target or "," in str(source):
            raise ExecutorError(f"invalid mount target {mount.target!r}")
        options = f"type=bind,source={source},target={mount.target}"
        return options + (",readonly" if mount.read_only else "")

    def image_present(self, image: str) -> bool:
        """Whether ``image`` is in the local image store."""
        docker = shutil.which(self._docker)
        if docker is None:
            return False
        proc = subprocess.run(  # noqa: S603 - fixed argv, validated image reference
            [docker, "image", "inspect", "--format", "{{.Id}}", "--", image],
            capture_output=True, text=True, timeout=30, check=False, env=_docker_env(),
        )  # fmt: skip
        return proc.returncode == 0

    def pull(self, image: str, *, timeout: float = 900.0) -> None:
        """Pull a digest-pinned image (an explicit, user-initiated network action)."""
        if not _DIGEST_REF.match(image):
            raise ExecutorError(f"image '{image}' must be pinned by digest")
        docker = shutil.which(self._docker)
        if docker is None:
            raise ExecutorUnavailableError("docker CLI not found")
        proc = subprocess.run(  # noqa: S603 - fixed argv, validated image reference
            [docker, "pull", "--quiet", image], capture_output=True, text=True,
            timeout=timeout, check=False, env=_docker_env(),
        )  # fmt: skip
        if proc.returncode != 0:
            raise ExecutorUnavailableError(
                f"pull of {image} failed: {redact(proc.stderr.strip())[-300:]}"
            )

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        """Run ``spec`` in a fresh container."""
        docker = shutil.which(self._docker)
        if docker is None:
            raise ExecutorUnavailableError("docker CLI not found")
        name = f"ai-coding-{uuid.uuid4().hex[:16]}"
        if spec.network and spec.egress is not None:
            async with _gateway(docker, name, spec.egress, _docker_env()) as gateway:
                result = await self._run(docker, name, spec, gateway.route)
            denied = gateway.denied()
            if denied:  # make the reason of a failed install visible in the step's output
                note = "\negress denied by policy: " + "; ".join(denied[:10]) + "\n"
                result = replace(result, stderr=result.stderr + note)
            return result
        return await self._run(docker, name, spec, None)

    async def _run(
        self, docker: str, name: str, spec: CommandSpec, egress: tuple[str, str] | None
    ) -> ExecutionResult:
        argv = [docker, *self.build_argv(spec, name, egress=egress)]

        def kill() -> None:
            subprocess.run(  # noqa: S603 - fixed argv
                [docker, "kill", name],
                capture_output=True,
                timeout=30,
                check=False,
                env=_docker_env(),
            )

        outcome = await run_process(
            argv,
            cwd=Path.cwd() if not spec.cwd.is_dir() else spec.cwd,
            env=_docker_env(),
            timeout=min(spec.timeout_seconds, float(self._limits.timeout_seconds)),
            max_output_bytes=spec.max_output_bytes,
            on_timeout=kill,
        )
        if outcome.exit_code == 125 and "Cannot connect to the Docker daemon" in outcome.stderr:
            raise ExecutorUnavailableError("Docker daemon is not reachable")
        if outcome.exit_code == 125 and (
            "No such image" in outcome.stderr or "Unable to find image" in outcome.stderr
        ):
            raise ExecutorUnavailableError(
                f"image {spec.image or self._cfg.image} is not present locally; "
                "run `ai-coding tools pull` (requires network access)"
            )
        return ExecutionResult(
            backend=self.backend,
            argv=tuple(argv),
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_seconds=outcome.duration_seconds,
            timed_out=outcome.timed_out,
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
        )


# What the docker CLI needs to reach the daemon, and nothing else (no secrets of the
# platform process): a remote daemon over TLS (DOCKER_TLS_VERIFY, DOCKER_CERT_PATH) or
# SSH (ssh://, SSH_AUTH_SOCK) and rootless Docker (XDG_RUNTIME_DIR) — S9, B-807.
DOCKER_CLIENT_ENV = (
    "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH", "DOCKER_API_VERSION", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR",
    "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP",
)  # fmt: skip


def _docker_env() -> dict[str, str]:
    import os  # noqa: PLC0415

    return {k: v for k, v in os.environ.items() if k in DOCKER_CLIENT_ENV}
