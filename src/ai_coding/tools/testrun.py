"""Project tests in the Docker sandbox (DEVELOP / MODERNIZE; ТЗ §10 Tests gate, §12).

Code written by an agent is untrusted, so it never runs on the host: the tree under
test is *copied* into a scratch directory that is the only writable mount of a
throw-away container (no network, read-only root, no capabilities, non-root).

Two phases:

* ``install`` (optional) — the only step that may use the network. It runs only if
  ``execution.docker.network`` is ``bridge`` *and* the egress policy allows every
  host the install needs; otherwise the run is SKIPPED with the reason. Python
  dependencies go to ``/deps`` (on ``PYTHONPATH``), Node modules into the copy.
* ``command`` — always without network.

Defaults per stack (overridable in ``quality_gates.tests``):

=========  ===========================================  ================================
stack      install                                      command
=========  ===========================================  ================================
python     ``pip install --target /deps pytest`` (+     ``python -m pytest -q``
           ``requirements*.txt``)
node       ``npm ci --ignore-scripts`` if dependencies  ``npm test`` if defined, else
                                                        ``node --test``
=========  ===========================================  ================================
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_coding.config.models import TestsGate
from ai_coding.executors.base import CommandSpec, Executor, ExecutorError, Mount
from ai_coding.security import redact
from ai_coding.tools.scratch import sandbox_scratch

# Official Docker Hub images served by Google's mirror (no Docker Hub rate limits);
# the digest fixes the content whatever the registry (NFR-05).
PYTHON_IMAGE = (  # python:3.12-slim
    "mirror.gcr.io/library/python@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e"
)
NODE_IMAGE = (  # node:22-slim
    "mirror.gcr.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d"
)
RUNTIME_IMAGES: dict[str, str] = {"python-runtime": PYTHON_IMAGE, "node-runtime": NODE_IMAGE}
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")
NPM_HOSTS = ("registry.npmjs.org",)
_NPM_DEFAULT_TEST = 'echo "Error: no test specified" && exit 1'
_SKIP_DIRS = {".git", ".ai-coding", "node_modules", ".venv", "venv", "__pycache__",
              ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build"}  # fmt: skip
_MAX_COPY_BYTES = 200 * 1024 * 1024
_OUTPUT_TAIL = 6000


class TestStatus(StrEnum):
    """Outcome of a sandbox test run (maps onto the Tests gate)."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TestPlan:
    """What to run for a project."""

    stack: str
    image: str
    command: tuple[str, ...]
    install: tuple[str, ...] | None = None
    install_hosts: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 900.0

    def describe(self) -> str:
        """Human-readable summary."""
        inst = f"install: {shlex.join(self.install)}; " if self.install else ""
        return f"{self.stack}: {inst}test: {shlex.join(self.command)}"


@dataclass(frozen=True, slots=True)
class TestRun:
    """Result of running the tests once."""

    status: TestStatus
    reason: str
    command: str
    exit_code: int | None = None
    passed: int | None = None
    failed: int | None = None
    duration_seconds: float = 0.0
    output: str = ""

    @property
    def ok(self) -> bool:
        """Only a real pass counts."""
        return self.status is TestStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        """JSON form (output already redacted)."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "command": self.command,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "duration_seconds": round(self.duration_seconds, 3),
            "output": self.output,
        }


# ------------------------------------------------------------------ planning --
def _python_detected(root: Path) -> bool:
    if any((root / f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg")):
        return True
    return any(root.glob("requirements*.txt")) or any(root.glob("tests/**/*.py"))


def _python_plan(root: Path) -> TestPlan:
    install = ["python", "-m", "pip", "install", "--quiet", "--no-cache-dir",
               "--disable-pip-version-check", "--target", "/deps", "pytest"]  # fmt: skip
    for req in sorted(root.glob("requirements*.txt")):
        install += ["-r", req.name]
    return TestPlan(
        stack="python",
        image=PYTHON_IMAGE,
        install=tuple(install),
        install_hosts=PYPI_HOSTS,
        command=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
        env={"PYTHONPATH": "/deps:/work/src:/work", "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _node_plan(root: Path) -> TestPlan:
    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    deps = bool(manifest.get("dependencies") or manifest.get("devDependencies"))
    install: tuple[str, ...] | None = None
    if deps:
        verb = "ci" if (root / "package-lock.json").is_file() else "install"
        install = ("npm", verb, "--ignore-scripts", "--no-audit", "--no-fund")
    script = (manifest.get("scripts") or {}).get("test")
    command = (
        ("npm", "test", "--silent")
        if script and script != _NPM_DEFAULT_TEST
        else ("node", "--test")
    )
    return TestPlan(stack="node", image=NODE_IMAGE, install=install,
                    install_hosts=NPM_HOSTS if install else (), command=command)  # fmt: skip


def plan_tests(root: Path, gate: TestsGate | None = None) -> TestPlan | None:
    """Test plan for the project at ``root`` (``None`` when no tests can be found)."""
    gate = gate or TestsGate()
    base: TestPlan | None
    if (root / "package.json").is_file():
        base = _node_plan(root)
    elif _python_detected(root):
        base = _python_plan(root)
    else:
        base = None
    if gate.command is None:
        if base is None:
            return None
        return TestPlan(
            stack=base.stack, image=gate.image or base.image, command=base.command,
            install=tuple(shlex.split(gate.install)) if gate.install else base.install,
            install_hosts=tuple(gate.install_hosts) or base.install_hosts,
            env=base.env, timeout_seconds=float(gate.timeout_seconds),
        )  # fmt: skip
    image = gate.image or (base.image if base else PYTHON_IMAGE)
    return TestPlan(
        stack=base.stack if base else "custom",
        image=image,
        command=tuple(shlex.split(gate.command)),
        install=tuple(shlex.split(gate.install)) if gate.install else None,
        install_hosts=tuple(gate.install_hosts),
        env=base.env if base else {},
        timeout_seconds=float(gate.timeout_seconds),
    )


# ------------------------------------------------------------------ parsing --
_PYTEST = re.compile(r"(\d+) (passed|failed|errors?)\b")
_NODE = re.compile(r"^# (pass|fail) (\d+)$", re.M)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests?", re.M)
_UNITTEST_FAIL = re.compile(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?")


def parse_counts(output: str) -> tuple[int | None, int | None]:
    """``(passed, failed)`` from pytest, ``node --test`` or unittest output."""
    node = {k: int(v) for k, v in _NODE.findall(output)}
    if node:
        return node.get("pass", 0), node.get("fail", 0)
    found = _PYTEST.findall(output)
    if found:
        counts: dict[str, int] = {}
        for n, kind in found:
            key = "failed" if kind != "passed" else "passed"
            counts[key] = counts.get(key, 0) + int(n)
        return counts.get("passed", 0), counts.get("failed", 0)
    ran = _UNITTEST_RAN.search(output)
    if ran:
        total = int(ran.group(1))
        fail = _UNITTEST_FAIL.search(output)
        bad = sum(int(x or 0) for x in fail.groups()) if fail else 0
        return total - bad, bad
    return None, None


# --------------------------------------------------------------------- run --
def copy_tree(
    source: Path,
    target: Path,
    *,
    skip: frozenset[str] | set[str] = frozenset(_SKIP_DIRS),
    max_bytes: int = _MAX_COPY_BYTES,
) -> None:
    """Copy ``source`` into ``target`` (no symlinks followed, size-capped)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if d not in skip]
        rel = Path(dirpath).relative_to(source)
        dest = target / rel
        dest.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src = Path(dirpath) / name
            if src.is_symlink():
                continue  # never follow links out of the tree
            total += src.stat().st_size
            if total > max_bytes:
                raise ExecutorError("project is too large to copy into the test sandbox")
            shutil.copy2(src, dest / name)


def open_permissions(root: Path, *, recursive: bool = True) -> None:
    """The sandbox user (1000) must be able to write into the scratch copy.

    Only for host-owned trees: entries the sandbox user created cannot be changed by
    the host user (use ``recursive=False`` for a directory that already holds them).
    """
    for path in [root, *root.rglob("*")] if recursive else [root]:
        mode = path.stat().st_mode
        extra = (
            stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO
            if path.is_dir()
            else (
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IROTH
                | stat.S_IWOTH
            )
        )
        path.chmod(mode | extra)


class SandboxTestRunner:
    """Runs a :class:`TestPlan` against a directory in the Docker sandbox."""

    def __init__(
        self,
        executor: Executor,
        *,
        network_allowed: Callable[[str], bool],
        sandbox_network: bool,
    ) -> None:
        self.executor = executor
        self._network_allowed = network_allowed
        self._sandbox_network = sandbox_network

    def _install_blocker(self, plan: TestPlan) -> str | None:
        if not plan.install:
            return None
        if not self._sandbox_network:
            return ("tests need a dependency install with network access, but "
                    "execution.docker.network is 'none'")  # fmt: skip
        denied = [h for h in plan.install_hosts if not self._network_allowed(h)]
        if denied or not plan.install_hosts:
            hosts = ", ".join(denied) or "(no install_hosts configured)"
            return f"dependency install needs egress to {hosts}, which security policy forbids"
        return None

    async def run(self, source: Path, plan: TestPlan) -> TestRun:
        """Copy ``source``, install (if allowed) and run the tests."""
        command = shlex.join(plan.command)
        blocker = self._install_blocker(plan)
        if blocker:
            return TestRun(TestStatus.SKIPPED, blocker, command)
        started = time.monotonic()
        async with sandbox_scratch(self.executor, "ai-coding-tests-") as scratch:
            tmp = scratch.path
            scratch.used(plan.image)
            work, deps = Path(tmp) / "work", Path(tmp) / "deps"
            copy_tree(source, work)
            deps.mkdir()
            open_permissions(Path(tmp))
            mounts = (Mount(work, "/work", read_only=False), Mount(deps, "/deps", read_only=False))
            if plan.install:
                result = await self.executor.run(CommandSpec(
                    argv=plan.install, cwd=work, image=plan.image, mounts=mounts, workdir="/work",
                    network=True, env=dict(plan.env), timeout_seconds=plan.timeout_seconds,
                ))  # fmt: skip
                if result.exit_code != 0:
                    tail = redact((result.stdout + result.stderr)[-_OUTPUT_TAIL:])
                    return TestRun(
                        TestStatus.ERROR, f"dependency install failed (exit {result.exit_code})",
                        command, result.exit_code, duration_seconds=time.monotonic() - started,
                        output=tail,
                    )  # fmt: skip
            result = await self.executor.run(CommandSpec(
                argv=plan.command, cwd=work, image=plan.image, mounts=mounts, workdir="/work",
                network=False, env=dict(plan.env), timeout_seconds=plan.timeout_seconds,
                ok_exit_codes=frozenset(range(256)),
            ))  # fmt: skip
        output = redact((result.stdout + "\n" + result.stderr).strip()[-_OUTPUT_TAIL:])
        passed, failed = parse_counts(output)
        duration = time.monotonic() - started
        if result.timed_out:
            return TestRun(TestStatus.FAILED, f"tests timed out after {plan.timeout_seconds:.0f}s",
                           command, result.exit_code, passed, failed, duration, output)  # fmt: skip
        if plan.command[:3] == ("python", "-m", "pytest") and result.exit_code == 5:
            return TestRun(TestStatus.SKIPPED, "pytest collected no tests", command, 5,
                           0, 0, duration, output)  # fmt: skip
        if result.exit_code == 0:
            return TestRun(TestStatus.PASSED, "tests passed", command, 0, passed, failed,
                           duration, output)  # fmt: skip
        return TestRun(TestStatus.FAILED, f"tests failed (exit {result.exit_code})", command,
                       result.exit_code, passed, failed, duration, output)  # fmt: skip
