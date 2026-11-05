"""DEVELOP end-to-end (S6, FR-04, AC-03, B-503): issue → loop → commit → verify → PR."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ai_coding.domain.enums import ExecutionBackend, Mode, RunState
from ai_coding.executors.base import CommandSpec, ExecutionResult
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.github import GitHubClient, GitHubHost, TokenAuth
from ai_coding.orchestrator import Orchestrator, TaskPlanner
from ai_coding.tools.testrun import NODE_IMAGE
from ai_coding.workflows.develop import DevelopRequest, branch_for, build_develop
from ai_coding.workflows.implement import ImplementSettings, unified_diff
from tests.fake_github import FakeGitHub
from tests.fake_llm import Error, FakeLLM
from tests.helpers import make_config, make_orchestrator
from tests.integration.conftest import require_images

pytestmark = pytest.mark.integration

PROJECT = {
    "package.json": json.dumps(
        {"name": "calc", "type": "module", "scripts": {"test": "node --test"}}
    )
    + "\n",
    "src/sum.js": "export function sum(a, b) {\n  return a + b;\n}\n",
    "test/sum.test.js": (
        'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
        'import { sum } from "../src/sum.js";\n\ntest("sum", () => assert.equal(sum(2, 3), 5));\n'
    ),
}
TEST_MUL = (
    'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
    'import { multiply } from "../src/multiply.js";\n\n'
    'test("multiply", () => assert.equal(multiply(2, 3), 6));\n'
)


def dev_output(body: str, *, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "summary": "Add multiply()",
        "plan": ["Add src/multiply.js", "Cover it with a test"],
        "changes": [
            {
                "path": "src/multiply.js",
                "action": "create",
                "content": f"export function multiply(a, b) {{\n  return {body};\n}}\n",
                "reason": "new function",
            },
            {
                "path": "test/multiply.test.js",
                "action": "create",
                "content": TEST_MUL,
                "reason": "test",
            },
            *(extra or []),
        ],
        "tests": ["test/multiply.test.js"],
        "notes": [],
        "confidence": 0.8,
    }


GOOD, BUGGY = dev_output("a * b"), dev_output("a + b")
ISSUE = {
    "number": 7,
    "title": "Add multiply function",
    "body": "We need multiply(a, b).",
    "html_url": "https://github.com/acme/demo/issues/7",
    "state": "open",
    "labels": [],
}


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


@pytest.fixture
def project(tmp_path: Path, git_env: None) -> LocalGitRepository:
    repo = LocalGitRepository.init(tmp_path / "calc")
    for rel, text in PROJECT.items():
        (repo.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo.root / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo.root, check=True)
    return repo


def config(fake: FakeLLM, **overrides: Any) -> Any:
    return make_config(
        Mode.DEVELOP,
        ai__enabled=True,
        ai__provider="ollama",
        ai__model="local-model",
        ai__base_url=f"{fake.url}/v1",
        **overrides,
    )


@dataclass
class FakeSandbox:
    """Executor double: 'tests pass' iff multiply is implemented correctly."""

    backend = ExecutionBackend.DOCKER
    specs: list[CommandSpec] = field(default_factory=list)

    def available(self) -> bool:
        return True

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        self.specs.append(spec)
        work = next(m.source for m in spec.mounts if m.target == "/work")
        impl = work / "src" / "multiply.js"
        ok = impl.is_file() and "a * b" in impl.read_text(encoding="utf-8")
        out = "# pass 2\n# fail 0\n" if ok else "# pass 1\n# fail 1\nnot ok 2 - multiply\n"
        return ExecutionResult(ExecutionBackend.DOCKER, spec.argv, 0 if ok else 1, out, "", 0.1)


async def drive(orch: Orchestrator, run_id: Any) -> tuple[Any, list[str]]:
    status = await orch.execute(run_id)
    approved: list[str] = []
    for _ in range(5):
        if status.state is not RunState.AWAITING_APPROVAL:
            break
        approved += [a.operation for a in orch.decide(run_id, approve=True)]
        status = await orch.resume(run_id)
    return status, approved


# ------------------------------------------------------------------ e2e --
@pytest.mark.docker
@pytest.mark.acceptance("AC-03")
async def test_develop_fix_loop_with_real_sandbox_tests(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    """AC-03: the first attempt fails its tests, the second passes; one commit, on a branch."""
    require_images(NODE_IMAGE)
    fake_llm.scripts["developer"] = [BUGGY, GOOD]
    request = DevelopRequest(title=ISSUE["title"], body=ISSUE["body"], issue_number=7)
    registry, steps = build_develop(request)
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert approved == []  # nothing critical was touched
    impl = orch.task_output(run_id, "develop.implement")
    assert impl is not None and impl["iterations"] == 2
    assert impl["tests"]["status"] == "passed" and impl["tests"]["passed"] == 2
    assert impl["branch"] == "ai/7-add-multiply-function"
    # The failing test output of iteration 1 was fed back to the developer.
    feedback = json.dumps(fake_llm.requests[2]["body"]["messages"][1]["content"])
    assert "fail 1" in feedback or "not ok" in feedback
    verify = orch.task_output(run_id, "develop.verify")
    assert verify is not None and verify["tests"]["status"] == "passed"
    # main untouched; the commit sits on the work branch with the run trailer.
    assert project.list_files("main") == sorted(PROJECT)
    assert "src/multiply.js" in project.list_files("ai/7-add-multiply-function")
    assert project.find_commits_by_trailer("AI-Coding-Run", str(run_id))
    assert project.current_branch() == "main" and project.status().is_clean


# ------------------------------------------------------------- loop rules --
async def test_review_blocking_finding_drives_another_iteration(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    blocking = {
        "summary": "bug",
        "verdict": "request_changes",
        "findings": [
            {
                "file": "src/multiply.js",
                "line": 2,
                "severity": "high",
                "category": "bug",
                "message": "overflow not handled",
                "suggestion": "use BigInt",
            }
        ],
    }
    fixed = dev_output("a * b /* overflow: inputs are validated by callers */")
    fake_llm.scripts["developer"] = [GOOD, fixed]
    fake_llm.scripts["reviewer"] = [blocking]  # second review: default approve
    registry, steps = build_develop(DevelopRequest(title="Add multiply"), executor=FakeSandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status, _ = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    impl = orch.task_output(run_id, "develop.implement")
    assert impl is not None and impl["iterations"] == 2
    second_dev = fake_llm.requests[2]["body"]["messages"][1]["content"]
    assert "overflow not handled" in second_dev


async def test_advisory_review_does_not_block_after_last_iteration_unless_policy(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    blocking = {
        "summary": "bug",
        "verdict": "request_changes",
        "findings": [
            {
                "file": "src/multiply.js",
                "line": 1,
                "severity": "critical",
                "category": "security",
                "message": "x",
                "suggestion": "y",
            }
        ],
    }
    for policy, expected in ((False, RunState.COMPLETED), (True, RunState.FAILED)):
        fake_llm.scripts["developer"] = [GOOD]
        fake_llm.scripts["reviewer"] = [blocking]
        cfg = config(fake_llm, quality_gates__review__ai_review_blocking=policy)
        request = DevelopRequest(
            title="Add multiply",
            branch=f"ai/m-{policy}",
            settings=ImplementSettings(max_iterations=1),
        )
        registry, steps = build_develop(request, executor=FakeSandbox())
        orch = make_orchestrator(project.root, list(registry.all()), config=cfg)
        status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
        assert status.state is expected, status.error
    assert "1 critical/high finding" in (status.error or "")


async def test_failing_tests_after_max_iterations_fail_the_run(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    fake_llm.scripts["developer"] = [BUGGY, BUGGY]
    request = DevelopRequest(title="Add multiply", settings=ImplementSettings(max_iterations=2))
    registry, steps = build_develop(request, executor=FakeSandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status, _ = await drive(orch, run_id)
    assert status.state is RunState.FAILED
    assert "not accepted after 2 iteration(s)" in (status.error or "")
    assert not project.branch_exists("ai/add-multiply")
    artifacts = [a["name"] for a in status.summary.artifacts]
    assert {"iteration-1.json", "iteration-2.json"} <= set(artifacts)


async def test_skipped_tests_are_rejected_in_strict_mode(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    @dataclass
    class NoDocker(FakeSandbox):
        def available(self) -> bool:
            return False

    fake_llm.scripts["developer"] = [GOOD, GOOD]
    for strict, expected in ((False, RunState.COMPLETED), (True, RunState.FAILED)):
        request = DevelopRequest(
            title="Add multiply",
            branch=f"ai/s-{strict}",
            settings=ImplementSettings(max_iterations=1, strict=strict),
        )
        registry, steps = build_develop(request, executor=NoDocker())
        orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
        status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
        assert status.state is expected, status.error
    assert "Docker sandbox unavailable" in (status.error or "")


async def test_tests_are_required_and_paths_are_confined(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    no_tests = dev_output("a * b")
    no_tests["changes"] = no_tests["changes"][:1]
    no_tests["tests"] = []
    escaping = dev_output(
        "a * b",
        extra=[
            {"path": ".git/hooks/post-commit", "action": "create", "content": "x", "reason": "evil"}
        ],
    )
    fake_llm.scripts["developer"] = [no_tests, escaping, GOOD]
    registry, steps = build_develop(DevelopRequest(title="Add multiply"), executor=FakeSandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.COMPLETED, status.error
    repairs = [
        r["body"]["messages"][-1]["content"] for r in fake_llm.requests if r["agent"] == "developer"
    ][1:]
    assert "every change must come with tests" in repairs[0]
    assert "inside a protected directory" in repairs[1]


async def test_critical_files_and_deletions_need_approval(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    out = dev_output(
        "a * b",
        extra=[
            {"path": "src/sum.js", "action": "delete", "reason": "unused"},
            {
                "path": ".github/workflows/ci.yml",
                "action": "create",
                "content": "on: push\n",
                "reason": "ci",
            },
        ],
    )
    fake_llm.scripts["developer"] = [out]
    registry, steps = build_develop(DevelopRequest(title="Add multiply"), executor=FakeSandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert approved == ["delete", "critical_file_change"]
    files = project.list_files("ai/add-multiply")
    assert "src/sum.js" not in files and ".github/workflows/ci.yml" in files


async def test_unseen_file_is_shown_before_it_is_rewritten(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    edit = dev_output(
        "a * b",
        extra=[
            {
                "path": "src/sum.js",
                "action": "modify",
                "content": "export const sum = (a, b) => a + b;\n",
                "reason": "style",
            }
        ],
    )
    fake_llm.scripts["developer"] = [edit, edit]
    request = DevelopRequest(title="Add multiply")  # the query does not select src/sum.js
    registry, steps = build_develop(request, executor=FakeSandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.COMPLETED, status.error
    dev_calls = [r for r in fake_llm.requests if r["agent"] == "developer"]
    assert len(dev_calls) == 2
    assert "export function sum" in dev_calls[1]["body"]["messages"][1]["content"]


@pytest.mark.acceptance("AC-03")
async def test_develop_opens_pr_with_report_body(
    project: LocalGitRepository, fake_llm: FakeLLM, tmp_path: Path
) -> None:
    gh = FakeGitHub().start()
    try:
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", bare.as_uri()], cwd=project.root, check=True
        )
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=project.root, check=True)
        host = GitHubHost(GitHubClient(TokenAuth(gh.token), api_url=gh.url), gh.repo)

        class Shas(dict[str, str]):
            def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
                return project.rev_parse(key) if project.branch_exists(key) else default

        gh.head_shas = Shas()
        fake_llm.scripts["developer"] = [GOOD]
        request = DevelopRequest(title=ISSUE["title"], body=ISSUE["body"], issue_number=7)
        registry, steps = build_develop(
            request, pull_request=(host, lambda: None, "localhost"), executor=FakeSandbox()
        )
        cfg = config(
            fake_llm, security__network="allowlist", security__egress_allowlist=["localhost"]
        )
        orch = make_orchestrator(project.root, list(registry.all()), config=cfg)
        status, approved = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
        assert status.state is RunState.COMPLETED, status.error
        assert approved == ["pr_create"]
        pr = gh.pulls[1]
        assert pr["title"] == "#7: Add multiply function"
        assert "Closes #7" in pr["body"] and "PASSED" in pr["body"]
        assert "`src/multiply.js` (create)" in pr["body"] and pr["auto_merge"] is None
    finally:
        gh.stop()


async def test_provider_failure_is_reported(project: LocalGitRepository, fake_llm: FakeLLM) -> None:
    fake_llm.scripts["developer"] = [Error(401, "bad key")]
    registry, steps = build_develop(DevelopRequest(title="Add multiply"), executor=FakeSandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED and "bad key" in (status.error or "")


def test_helpers(tmp_path: Path) -> None:
    assert branch_for("Fix: crash on empty input!", 12) == "ai/12-fix-crash-on-empty-input"
    assert branch_for("???") == "ai/change"
    (tmp_path / "a.txt").write_text("1\n2\n", encoding="utf-8")
    diff = unified_diff(
        tmp_path,
        [
            {"path": "a.txt", "action": "modify", "content": "1\n3\n"},
            {"path": "b.txt", "action": "create", "content": "new\n"},
            {"path": "a.txt", "action": "delete"},
        ],
    )
    assert "-2\n+3\n" in diff and "+++ b/b.txt" in diff and "+++ /dev/null" in diff
