"""B-801 (sandbox escape tests, ТЗ §12): code under test runs in the Docker sandbox and
tries to get out. Each probe prints a token; the sandbox must hold for all of them.

The probes run as the untrusted project code would: from a shell in the container,
with the project mounted read-only and a writable /tmp.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.config.models import ResourceLimits
from ai_coding.executors import CommandSpec, DockerSandboxExecutor, Mount
from tests.integration.conftest import ALPINE, require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]
HOST_SECRET = "HOST_ONLY_SECRET_9c4e"

PROBES = {
    # identity and privileges
    "uid": "id -u",
    "no_new_privs": "grep -E '^NoNewPrivs:' /proc/self/status | tr -d '\\t '",
    "cap_eff": "grep -E '^CapEff:' /proc/self/status | tr -d '\\t '",
    "cap_prm": "grep -E '^CapPrm:' /proc/self/status | tr -d '\\t '",
    "cap_bnd": "grep -E '^CapBnd:' /proc/self/status | tr -d '\\t '",
    "seccomp": "grep -E '^Seccomp:' /proc/self/status | tr -d '\\t '",
    # host visibility
    "pids": "ls -d /proc/[0-9]* | wc -l",
    "docker_sock": "ls /var/run/docker.sock /run/docker.sock 2>/dev/null | wc -l",
    "docker_env": "env | grep -c -E '^DOCKER_HOST=|HOST_ONLY' || true",
    "host_secret_env": f"env | grep -c {HOST_SECRET} || true",
    # kernel and devices
    "mount": "mount -t tmpfs none /tmp/m 2>/dev/null && echo MOUNTED || echo DENIED",
    "mknod": "mknod /tmp/dev c 1 1 2>/dev/null && echo MKNOD || echo DENIED",
    "dev_mem": "ls /dev/mem /dev/kmem /dev/sda 2>/dev/null | grep -q . && echo DEV || echo NONE",
    "sysctl": "echo 1 > /proc/sys/kernel/sysrq 2>/dev/null && echo SYSCTL || echo DENIED",
    "sys_ro": "touch /sys/kernel/x 2>/dev/null && echo SYS-RW || echo DENIED",
    "chroot": "chroot / true 2>/dev/null && echo CHROOT || echo DENIED",
    "ptrace": "cat /proc/1/environ >/dev/null 2>&1 && echo OWN || echo DENIED",
    # file system
    "root_fs": "touch /etc/x 2>/dev/null && echo RW || echo RO",
    "passwd": "echo 'x::0:0::/:/bin/sh' >> /etc/passwd 2>/dev/null && echo RW || echo RO",
    "project": "touch /src/new 2>/dev/null && echo RW || echo RO",
    "symlink": "cat /src/escape 2>/dev/null || echo NOT-FOUND",
    "tmp_exec": "cp /bin/busybox /tmp/b; /tmp/b true 2>/dev/null && echo EXEC || echo NOEXEC",
    # resources
    "pids_max": "cat /sys/fs/cgroup/pids.max || cat /sys/fs/cgroup/pids/pids.max",
    "mem_max": "cat /sys/fs/cgroup/memory.max || cat /sys/fs/cgroup/memory/memory.limit_in_bytes",
    "network": "(wget -q -T 2 -O- http://1.1.1.1 >/dev/null 2>&1 && echo NET) || echo NO-NET",
    "interfaces": "ls /sys/class/net | tr '\\n' ' '",
}


@pytest.fixture
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    require_images(ALPINE)
    monkeypatch.setenv("AI_CODING_PROBE_SECRET", HOST_SECRET)  # in the host process only
    outside = tmp_path / "outside.txt"
    outside.write_text(HOST_SECRET, encoding="utf-8")  # a host file outside the mount
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("print(1)\n", encoding="utf-8")
    (project / "escape").symlink_to(outside)  # absolute link pointing out of the project
    for path in (tmp_path, project):
        path.chmod(0o755)
    script = "\n".join(f'echo "{k}=$( {{ {v}; }} )"' for k, v in PROBES.items())
    sandbox = DockerSandboxExecutor(limits=ResourceLimits(memory_mb=256, cpus=0.5))
    result = __import__("asyncio").run(sandbox.run(CommandSpec(
        argv=("sh", "-c", script), cwd=project, image=ALPINE,
        mounts=(Mount(project, "/src"),), timeout_seconds=60,
    )))  # fmt: skip
    assert result.exit_code == 0, result.stderr
    assert HOST_SECRET not in result.stdout + result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_no_privileges(results: dict[str, str]) -> None:
    assert results["uid"] == "1000"
    assert results["no_new_privs"] == "NoNewPrivs:1"
    zero = "0000000000000000"
    assert results["cap_eff"] == f"CapEff:{zero}" and results["cap_prm"] == f"CapPrm:{zero}"
    assert results["cap_bnd"] == f"CapBnd:{zero}"
    assert results["seccomp"] == "Seccomp:2"  # the default seccomp profile filters syscalls


def test_no_view_of_the_host(results: dict[str, str]) -> None:
    assert int(results["pids"]) <= 5  # own PID namespace: only this shell and its children
    assert results["docker_sock"] == "0" and results["docker_env"] == "0"
    assert results["host_secret_env"] == "0"  # the host environment is not inherited


def test_kernel_and_devices_are_out_of_reach(results: dict[str, str]) -> None:
    for probe in ("mount", "mknod", "sysctl", "sys_ro", "chroot"):
        assert results[probe] == "DENIED", (probe, results[probe])
    assert results["dev_mem"] == "NONE"


def test_file_system_stays_closed(results: dict[str, str]) -> None:
    assert results["root_fs"] == "RO" and results["passwd"] == "RO"
    assert results["project"] == "RO"  # the project is mounted read-only
    assert results["symlink"] == "NOT-FOUND"  # an absolute link does not reach the host
    assert results["tmp_exec"] == "NOEXEC"  # /tmp is noexec: no dropped binaries


def test_resources_and_network_are_limited(results: dict[str, str]) -> None:
    assert results["pids_max"] == "256"  # no fork bomb
    assert int(results["mem_max"]) == 256 * 1024 * 1024
    assert results["network"] == "NO-NET" and results["interfaces"].split() == ["lo"]
