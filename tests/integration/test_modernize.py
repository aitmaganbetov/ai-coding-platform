"""MODERNIZE (S6, FR-05): baseline, impact analysis, plan approval, small steps, rollback."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ai_coding.domain.enums import ExecutionBackend, Mode, RunState
from ai_coding.executors.base import CommandSpec, ExecutionResult
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator import Orchestrator, TaskPlanner
from ai_coding.workflows.modernize import (
    ModernizeRequest,
    RollbackRequest,
    build_modernize,
    build_rollback,
    next_step_id,
    plan_version,
    render_plan,
)
from tests.fake_llm import FakeLLM
from tests.helpers import make_config, make_orchestrator
from tests.integration.test_develop import PROJECT

pytestmark = pytest.mark.integration

PLAN = {
    "goal": "Convert functions to arrow syntax",
    "baseline_assessment": "Tests are green; two small modules.",
    "impact": {
        "files": ["src/sum.js", "test/sum.test.js"],
        "modules": ["src"],
        "risks": [{"risk": "export shape changes", "mitigation": "keep named exports"}],
    },
    "steps": [
        {
            "id": "M-01",
            "title": "Convert sum",
            "description": "Arrow function for sum",
            "files": ["src/sum.js"],
            "verification": "npm test",
            "rollback": "revert the PR",
            "risk": "low",
        },
        {
            "id": "M-02",
            "title": "Document style",
            "description": "Note the convention",
            "files": ["README.md"],
            "verification": "npm test",
            "rollback": "revert the PR",
            "risk": "low",
        },
    ],
    "rollback_strategy": "Each step is one PR; revert it.",
    "confidence": 0.7,
}
STEP1 = {
    "summary": "sum as an arrow function",
    "plan": ["Rewrite sum", "Assert the export still works"],
    "changes": [
        {
            "path": "src/sum.js",
            "action": "modify",
            "content": "export const sum = (a, b) => a + b;\n",
            "reason": "style",
        },
        {
            "path": "test/sum.test.js",
            "action": "modify",
            "content": PROJECT["test/sum.test.js"] + 'test("sum is a function", () => '
            'assert.equal(typeof sum, "function"));\n',
            "reason": "test",
        },
    ],
    "tests": ["test/sum.test.js"],
    "notes": [],
    "confidence": 0.8,
}
STEP2 = {
    "summary": "document the convention",
    "plan": ["Add README"],
    "changes": [
        {
            "path": "README.md",
            "action": "create",
            "content": "# calc\n\nArrow functions.\n",
            "reason": "docs",
        },
        {
            "path": "test/readme.test.js",
            "action": "create",
            "content": 'import { test } from "node:test";\ntest("ok", () => {});\n',
            "reason": "t",
        },
    ],
    "tests": ["test/readme.test.js"],
    "notes": [],
    "confidence": 0.8,
}


@dataclass
class Sandbox:
    """Tests pass unless the tree contains a ``BROKEN`` marker in src/sum.js."""

    backend = ExecutionBackend.DOCKER
    runs: list[Path] = field(default_factory=list)

    def available(self) -> bool:
        return True

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        work = next(m.source for m in spec.mounts if m.target == "/work")
        self.runs.append(work)
        broken = "BROKEN" in (work / "src" / "sum.js").read_text(encoding="utf-8")
        out = "# pass 0\n# fail 1\n" if broken else "# pass 2\n# fail 0\n"
        return ExecutionResult(ExecutionBackend.DOCKER, spec.argv, int(broken), out, "", 0.1)


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


def config(fake: FakeLLM) -> Any:
    return make_config(
        Mode.MODERNIZE,
        ai__enabled=True,
        ai__provider="ollama",
        ai__model="local-model",
        ai__base_url=f"{fake.url}/v1",
    )


async def drive(orch: Orchestrator, run_id: Any) -> tuple[Any, list[str]]:
    status = await orch.execute(run_id)
    approved: list[str] = []
    for _ in range(5):
        if status.state is not RunState.AWAITING_APPROVAL:
            break
        approved += [a.operation for a in orch.decide(run_id, approve=True)]
        status = await orch.resume(run_id)
    return status, approved


async def test_plan_approve_and_first_step(project: LocalGitRepository, fake_llm: FakeLLM) -> None:
    fake_llm.scripts["migration"] = [PLAN]
    fake_llm.scripts["developer"] = [STEP1]
    registry, steps = build_modernize(ModernizeRequest(goal=PLAN["goal"]), executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    first = await orch.execute(run_id)
    # Nothing changes before the plan is approved.
    assert first.state is RunState.AWAITING_APPROVAL
    assert "migration plan p1-" in first.approvals[0]["reason"]
    assert fake_llm.agents_called() == ["migration"]
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert approved == ["spec_approval"]
    baseline = orch.task_output(run_id, "modernize.baseline")
    assert baseline is not None and baseline["tests"]["status"] == "passed"
    out = orch.task_output(run_id, "modernize.step")
    assert out is not None and out["step"]["id"] == "M-01" and out["next_step"] == "M-02"
    assert out["branch"] == "ai/modernize-convert-functions-to-arrow-syntax-m-01"
    assert "## Шаг модернизации M-01" in out["pr_body"] and "Откат: revert the PR" in out["pr_body"]
    # The planner saw the baseline and the relevant files (impact analysis input).
    prompt = fake_llm.requests[0]["body"]["messages"][1]["content"]
    assert "baseline_tests" in prompt and "src/sum.js" in prompt
    names = {a["name"] for a in status.summary.artifacts}
    assert {"migration-plan.json", "migration-plan.md"} <= names
    assert project.file_at(out["branch"], "src/sum.js").startswith("export const sum")
    assert project.file_at("main", "src/sum.js") == PROJECT["src/sum.js"]


async def test_red_baseline_stops_before_planning(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    (project.root / "src/sum.js").write_text("// BROKEN\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "break"], cwd=project.root, check=True)
    registry, steps = build_modernize(ModernizeRequest(goal="x"), executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED
    assert "baseline tests are red" in (status.error or "") and fake_llm.requests == []


async def test_regression_is_not_accepted(project: LocalGitRepository, fake_llm: FakeLLM) -> None:
    broken = {
        **STEP1,
        "changes": [{**STEP1["changes"][0], "content": "// BROKEN\n"}, STEP1["changes"][1]],
    }
    request = ModernizeRequest(goal="x", plan=PLAN, plan_version="p1-aaaaaaaa")
    fake_llm.scripts["developer"] = [broken] * 3
    registry, steps = build_modernize(request, executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED and "tests failed" in (status.error or "")


async def test_continue_with_next_step_and_rollback(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    fake_llm.scripts["developer"] = [STEP2]
    request = ModernizeRequest(
        goal=PLAN["goal"], plan=PLAN, plan_version="p1-aaaaaaaa", step="M-02"
    )
    registry, steps = build_modernize(request, executor=Sandbox())
    assert "modernize.plan" not in {s.key for s in steps}  # approved plan is reused
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error and approved == []
    out = orch.task_output(run_id, "modernize.step")
    assert out is not None and out["step"]["id"] == "M-02" and out["next_step"] is None
    branch = out["branch"]

    # Not merged yet: rollback means closing the PR; nothing is reverted.
    rb_registry, rb_steps = build_rollback(RollbackRequest(str(run_id), source_branch=branch))
    rb = make_orchestrator(project.root, list(rb_registry.all()), config=config(fake_llm))
    rb_id = rb.create_run(TaskPlanner().build(rb_steps))
    rb_status, _ = await drive(rb, rb_id)
    assert rb_status.state is RunState.COMPLETED
    result = rb.task_output(rb_id, "rollback.revert")
    assert result is not None and result["status"] == "not_merged"
    assert f"delete branch {branch}" in result["advice"]

    # A human merges the PR (merge commit), then the step is rolled back.
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "Merge step", branch], cwd=project.root, check=True
    )
    assert "README.md" in project.list_files("main")
    rb_id2 = rb.create_run(TaskPlanner().build(rb_steps), goal="rollback")
    rb_status2, _ = await drive(rb, rb_id2)
    assert rb_status2.state is RunState.COMPLETED, rb_status2.error
    reverted = rb.task_output(rb_id2, "rollback.revert")
    assert reverted is not None and reverted["status"] == "reverted"
    revert_branch = reverted["branch"]
    assert revert_branch == f"ai/revert-{str(run_id)[-12:]}"
    assert "README.md" not in project.list_files(revert_branch)
    assert project.list_files(revert_branch) == project.list_files("main~1")
    assert project.commits_with_trailer("AI-Coding-Reverts", str(run_id), revert_branch)


async def test_rejected_plan_makes_no_change(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    fake_llm.scripts["migration"] = [PLAN]
    registry, steps = build_modernize(ModernizeRequest(goal=PLAN["goal"]), executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    assert (await orch.execute(run_id)).state is RunState.AWAITING_APPROVAL
    orch.decide(run_id, approve=False)
    assert (await orch.resume(run_id)).state is RunState.CANCELLED
    assert project.branches() == ["main"]


async def test_unknown_step_and_invalid_plan(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    request = ModernizeRequest(goal="x", plan=PLAN, plan_version="p1", step="M-09")
    registry, steps = build_modernize(request, executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED and "no step M-09" in (status.error or "")
    bad = {**PLAN, "impact": {**PLAN["impact"], "files": ["src/ghost.js"]}}
    fake_llm.scripts["migration"] = [bad, bad, bad]
    registry, steps = build_modernize(ModernizeRequest(goal="y"), executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED and "src/ghost.js, which does not exist" in (
        status.error or ""
    )


def test_plan_helpers() -> None:
    v1 = plan_version(PLAN)
    assert v1.startswith("p1-") and plan_version(PLAN, v1).startswith("p2-")
    md = render_plan(PLAN, v1)
    assert "### M-01: Convert sum (риск: low)" in md and "- Откат: revert the PR" in md
    assert next_step_id(PLAN, "M-01") == "M-02" and next_step_id(PLAN, "M-02") is None
