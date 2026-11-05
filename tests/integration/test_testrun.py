"""Project tests in the Docker sandbox (S6): planning, parsing, install gating, real runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_coding.config.models import TestsGate as GateConfig
from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors import DockerSandboxExecutor
from ai_coding.executors.base import CommandSpec, ExecutionResult
from ai_coding.tools.testrun import (
    NODE_IMAGE,
    PYTHON_IMAGE,
    SandboxTestRunner,
    parse_counts,
    plan_tests,
)
from ai_coding.tools.testrun import (
    TestStatus as Status,
)
from tests.integration.conftest import require_images


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


NODE_OK = {
    "package.json": json.dumps({"name": "x", "type": "module", "scripts": {"test": "node --test"}}),
    "test/a.test.js": 'import {test} from "node:test";\nimport assert from "node:assert";\n'
    'test("one", () => assert.equal(1, 1));\n',
}
PY_UNITTEST = {
    "pyproject.toml": '[project]\nname = "p"\n',
    "tests/__init__.py": "",
    "tests/test_a.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
    "    def test_ok(self):\n        self.assertEqual(1, 1)\n",
}


# ------------------------------------------------------------------ plans --
def test_plans_per_stack(tmp_path: Path) -> None:
    node = plan_tests(write(tmp_path / "n", NODE_OK))
    assert node is not None and node.stack == "node" and node.image == NODE_IMAGE
    assert node.command == ("npm", "test", "--silent") and node.install is None
    bare = plan_tests(write(tmp_path / "b", {"package.json": "{}"}))
    assert bare is not None and bare.command == ("node", "--test")
    deps = plan_tests(
        write(
            tmp_path / "d",
            {
                "package.json": json.dumps({"devDependencies": {"x": "1"}}),
                "package-lock.json": "{}",
            },
        )
    )
    assert deps is not None and deps.install == (
        "npm",
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    )
    assert deps.install_hosts == ("registry.npmjs.org",)
    py = plan_tests(write(tmp_path / "p", {**PY_UNITTEST, "requirements-dev.txt": "x\n"}))
    assert py is not None and py.image == PYTHON_IMAGE and py.install is not None
    assert py.install[-2:] == ("-r", "requirements-dev.txt") and "pytest" in py.install
    assert py.env["PYTHONPATH"] == "/deps:/work/src:/work"
    assert plan_tests(tmp_path / "empty") is None
    custom = plan_tests(
        tmp_path / "p",
        GateConfig(
            command="make test",
            install="pip install -e .",
            install_hosts=["pypi.org"],
            timeout_seconds=60,
        ),
    )
    assert custom is not None and custom.command == ("make", "test")
    assert custom.install == ("pip", "install", "-e", ".") and custom.timeout_seconds == 60
    assert plan_tests(tmp_path / "empty", GateConfig(command="true")) is not None


def test_parse_counts() -> None:
    assert parse_counts("....\n3 passed, 1 failed, 2 errors in 0.1s") == (3, 3)
    assert parse_counts("# tests 4\n# pass 3\n# fail 1\n") == (3, 1)
    assert parse_counts("Ran 5 tests in 0.01s\n\nFAILED (failures=1, errors=1)") == (3, 2)
    assert parse_counts("Ran 2 tests in 0.01s\n\nOK") == (2, 0)
    assert parse_counts("nothing") == (None, None)


# --------------------------------------------------- install gating (fake) --
@dataclass
class Recorder:
    backend = ExecutionBackend.DOCKER
    results: list[ExecutionResult] = field(default_factory=list)
    specs: list[CommandSpec] = field(default_factory=list)

    def available(self) -> bool:
        return True

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        self.specs.append(spec)
        # The scratch copy is writable by the sandbox user and excludes VCS/state dirs.
        work = next(m.source for m in spec.mounts if m.target == "/work")
        assert not (work / ".git").exists() and not (work / ".ai-coding").exists()
        return self.results.pop(0)


def result(code: int, out: str = "") -> ExecutionResult:
    return ExecutionResult(ExecutionBackend.DOCKER, ("x",), code, out, "", 0.1)


async def test_install_is_gated_by_sandbox_network_and_egress(tmp_path: Path) -> None:
    root = write(tmp_path / "p", {**PY_UNITTEST, ".git/HEAD": "x", ".ai-coding/state": "x"})
    plan = plan_tests(root)
    assert plan is not None
    rec = Recorder()
    offline = SandboxTestRunner(rec, network_allowed=lambda h: True, sandbox_network=False)
    run = await offline.run(root, plan)
    assert run.status is Status.SKIPPED and "execution.docker.network" in run.reason
    denied = SandboxTestRunner(
        rec, network_allowed=lambda h: h != "files.pythonhosted.org", sandbox_network=True
    )
    run = await denied.run(root, plan)
    assert run.status is Status.SKIPPED and "files.pythonhosted.org" in run.reason
    assert rec.specs == []  # nothing ran
    rec.results = [result(0), result(0, "2 passed in 0.1s")]
    allowed = SandboxTestRunner(rec, network_allowed=lambda h: True, sandbox_network=True)
    run = await allowed.run(root, plan)
    assert run.status is Status.PASSED and run.passed == 2
    install, test = rec.specs
    assert install.network and not test.network
    assert install.egress and install.egress == tuple(install.egress)  # allowlisted hosts
    assert {m.target: m.read_only for m in test.mounts} == {"/work": False, "/deps": False}
    rec.results = [result(1, "ERROR: no such package")]
    broken = await allowed.run(root, plan)
    assert broken.status is Status.ERROR and "install failed" in broken.reason
    rec.results = [result(0), result(5, "no tests ran")]
    assert (await allowed.run(root, plan)).status is Status.SKIPPED


# ------------------------------------------------------- real sandbox runs --
@pytest.mark.docker
async def test_node_tests_in_sandbox(tmp_path: Path) -> None:
    require_images(NODE_IMAGE)
    runner = SandboxTestRunner(
        DockerSandboxExecutor(), network_allowed=lambda h: False, sandbox_network=False
    )
    root = write(tmp_path / "n", NODE_OK)
    ok = await runner.run(root, plan_tests(root))  # type: ignore[arg-type]
    assert ok.status is Status.PASSED and (ok.passed, ok.failed) == (1, 0), ok.output
    (root / "test/b.test.js").write_text(
        'import {test} from "node:test";\nimport assert from "node:assert";\n'
        'test("two", () => assert.equal(1, 2));\n',
        encoding="utf-8",
    )
    bad = await runner.run(root, plan_tests(root))  # type: ignore[arg-type]
    assert bad.status is Status.FAILED and (bad.passed, bad.failed) == (1, 1)
    assert not (root / "node_modules").exists()  # the source tree is never written


@pytest.mark.docker
async def test_python_unittest_in_sandbox_without_network(tmp_path: Path) -> None:
    require_images(PYTHON_IMAGE)
    runner = SandboxTestRunner(
        DockerSandboxExecutor(), network_allowed=lambda h: True, sandbox_network=False
    )
    root = write(
        tmp_path / "p",
        {
            **PY_UNITTEST,
            "tests/test_net.py": "import socket, unittest\n\n\nclass N(unittest.TestCase):\n"
            "    def test_no_network(self):\n"
            "        with self.assertRaises(OSError):\n"
            "            socket.create_connection(('1.1.1.1', 53), timeout=2)\n",
        },
    )
    plan = plan_tests(root, GateConfig(command="python -m unittest discover -v"))
    run = await runner.run(root, plan)  # type: ignore[arg-type]
    assert run.status is Status.PASSED, run.output
    assert (run.passed, run.failed) == (2, 0)
