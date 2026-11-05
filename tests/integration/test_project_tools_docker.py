"""S7 acceptance (AC-02 shape): stack tools run in the network-less sandbox, find planted
issues with file paths, never write the repository, and give reproducible results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_coding.executors import DockerSandboxExecutor
from ai_coding.tools.golang import GO_IMAGE, GOLANGCI_IMAGE, GolangciLintAdapter, GoTestAdapter
from ai_coding.tools.prepare import is_prepared
from ai_coding.tools.project import ProjectToolRunner
from ai_coding.tools.runner import ToolRunner
from ai_coding.tools.rust import RUST_IMAGE, CargoTestAdapter, ClippyAdapter
from ai_coding.tools.trivy import TRIVY_IMAGE, TrivyAdapter
from ai_coding.workflows.audit import snapshot_digest
from tests.integration.conftest import require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]

GO_FILES = {
    "go.mod": "module example.com/calc\n\ngo 1.22\n",
    "calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n\n"
    "func Sub(a, b int) int { return a + b }\n\nfunc helper() int { return 1 }\n",
    "calc_test.go": 'package calc\n\nimport "testing"\n\n'
    'func TestAdd(t *testing.T) { if Add(2, 2) != 4 { t.Fatal("add") } }\n\n'
    'func TestSub(t *testing.T) { if Sub(3, 1) != 2 { t.Fatal("sub") } }\n',
}
RUST_FILES = {
    "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 {\n    let unused = 1;\n    a + b\n}\n\n"
    "pub fn same(a: i32) -> bool {\n    a == a\n}\n\n"
    "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n"
    "    fn adds() {\n        assert_eq!(add(1, 1), 2);\n    }\n\n    #[test]\n"
    "    fn subs() {\n        assert_eq!(add(3, -1), 3);\n    }\n}\n",
}
LOCK = {
    "name": "app", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
    "packages": {
        "": {"name": "app", "version": "1.0.0", "dependencies": {"lodash": "4.17.15"}},
        "node_modules/lodash": {
            "version": "4.17.15",
            "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.15.tgz",
        },
    },
}  # fmt: skip


def make(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return root


def sandbox(root: Path) -> ProjectToolRunner:
    return ProjectToolRunner(DockerSandboxExecutor(), root, network_allowed=lambda _h: False,
                             sandbox_network=False, timeout_seconds=600)  # fmt: skip


async def test_go_test_and_golangci_lint(tmp_path: Path) -> None:
    require_images(GO_IMAGE, GOLANGCI_IMAGE)
    root = make(tmp_path / "go", GO_FILES)
    before = snapshot_digest(root)
    tests = await sandbox(root).run(GoTestAdapter())
    assert tests.succeeded, tests.error
    assert [f.rule_id for f in tests.findings] == ["go-test-failure"]
    assert "TestSub" in tests.findings[0].title and tests.coverage is not None
    assert tests.version and tests.version.startswith("1.")
    lint = await sandbox(root).run(GolangciLintAdapter())
    assert lint.succeeded, lint.error
    assert ("unused", "calc.go") in {(f.rule_id, f.path) for f in lint.findings}
    assert snapshot_digest(root) == before  # scratch copy: the repository is untouched


async def test_cargo_test_and_clippy(tmp_path: Path) -> None:
    require_images(RUST_IMAGE)
    root = make(tmp_path / "rs", RUST_FILES)
    before = snapshot_digest(root)
    tests = await sandbox(root).run(CargoTestAdapter())
    assert tests.succeeded, tests.error
    assert [(f.rule_id, f.path) for f in tests.findings] == [("cargo-test-failure", "src/lib.rs")]
    assert not (root / "Cargo.lock").exists() and snapshot_digest(root) == before
    if not is_prepared(ClippyAdapter()):
        pytest.skip("clippy toolchain not prepared (ai-coding tools pull -t clippy)")
    clippy = await sandbox(root).run(ClippyAdapter())
    assert clippy.succeeded, clippy.error
    assert {"unused_variables", "clippy::eq_op"} <= {f.rule_id for f in clippy.findings}


async def test_trivy_offline_is_reproducible(tmp_path: Path) -> None:
    require_images(TRIVY_IMAGE)
    if not is_prepared(TrivyAdapter()):
        pytest.skip("trivy DB not prepared (ai-coding tools pull -t trivy)")
    root = make(tmp_path / "node", {"package.json": json.dumps({"name": "app"}),
                                    "package-lock.json": json.dumps(LOCK),
                                    "Dockerfile": "FROM node:20\nUSER root\n"})  # fmt: skip
    runner = ToolRunner(DockerSandboxExecutor(), root)
    first = await runner.run(TrivyAdapter())
    assert first.succeeded, first.error
    assert ("CVE-2020-8203", "package-lock.json") in {(f.rule_id, f.path) for f in first.findings}
    assert first.metadata["db_updated_at"]  # the DB snapshot is recorded in the report
    second = await runner.run(TrivyAdapter())
    assert sorted(f.fingerprint for f in first.findings) == sorted(
        f.fingerprint for f in second.findings
    )
