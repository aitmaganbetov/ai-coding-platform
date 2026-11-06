"""MAINTAIN end-to-end (S7, B-610/B-612): sources → plan → approval → dependency upgrade
by the package manager + regression test → commit → verify → next item."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import ExecutionBackend, Mode, RunState
from ai_coding.executors.base import CommandSpec, ExecutionResult
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator import TaskPlanner
from ai_coding.workflows.maintain import MaintainRequest, build_maintain
from tests.fake_llm import FakeLLM
from tests.helpers import make_config, make_orchestrator
from tests.integration.test_create_cli import write_config
from tests.integration.test_develop import GOOD, PROJECT, drive

pytestmark = pytest.mark.integration
runner = CliRunner()

LOCK = {"name": "calc", "lockfileVersion": 3, "requires": True,
        "packages": {"": {"name": "calc", "dependencies": {"lodash": "4.17.15"}},
                     "node_modules/lodash": {"version": "4.17.15"}}}  # fmt: skip
FILES = {
    **PROJECT,
    "package.json": json.dumps(
        {
            "name": "calc",
            "type": "module",
            "scripts": {"test": "node --test"},
            "dependencies": {"lodash": "4.17.15"},
        }
    )
    + "\n",
    "package-lock.json": json.dumps(LOCK, indent=2) + "\n",
}
ISSUES = [{"id": "#7", "title": "Add multiply function", "body": "We need multiply(a, b)."}]
DEPENDENCIES = [{"id": "D1", "package": "lodash", "ecosystem": "npm", "version": "4.17.15",
                 "path": "package-lock.json", "fixed_versions": ["4.17.21"],
                 "advisories": ["CVE-2021-23337"], "severity": "high"}]  # fmt: skip
PLAN = {
    "summary": "Upgrade lodash first, then add multiply().",
    "items": [
        {"id": "M1", "title": "Add multiply()", "kind": "bug", "priority": "p1",
         "sources": ["#7"], "plan": "Implement multiply and test it.", "risk": "low",
         "files": ["src/multiply.js"],
         "regression_test": {"file": "test/multiply.test.js", "description": "2*3=6"}},
        {"id": "M2", "title": "Upgrade lodash", "kind": "dependency", "priority": "p0",
         "sources": ["D1"], "plan": "Upgrade to the fixed version.", "risk": "low",
         "files": ["package.json", "package-lock.json"],
         "upgrades": [{"source": "D1", "to_version": "4.17.21"}],
         "regression_test": {"file": "test/lodash.test.js",
                             "description": "template() rejects code injection"}},
    ],
    "deferred": [],
}  # fmt: skip
LODASH_TEST = {
    "summary": "Regression test for the lodash upgrade",
    "plan": ["Add a test that pins the fixed behaviour"],
    "changes": [{"path": "test/lodash.test.js", "action": "create", "reason": "regression",
                 "content": 'import { test } from "node:test";\n'
                            'test("lodash is patched", () => {});\n'}],
    "tests": ["test/lodash.test.js"], "notes": [], "confidence": 0.7,
}  # fmt: skip


@dataclass
class Sandbox:
    """Tests pass iff multiply is right (when present) and lodash is patched in the tree."""

    backend = ExecutionBackend.DOCKER
    specs: list[CommandSpec] = field(default_factory=list)

    def available(self) -> bool:
        return True

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        self.specs.append(spec)
        work = next(m.source for m in spec.mounts if m.target == "/work")
        if spec.argv[0] == "npm" and "install" in spec.argv:  # the package manager
            assert spec.network and "--ignore-scripts" in spec.argv
            for name in ("package.json", "package-lock.json"):
                f = work / name
                f.write_text(f.read_text(encoding="utf-8").replace("4.17.15", "4.17.21"),
                             encoding="utf-8")  # fmt: skip
            return ExecutionResult(ExecutionBackend.DOCKER, spec.argv, 0, "", "", 0.1)
        impl = work / "src" / "multiply.js"
        ok = (not impl.exists() or "a * b" in impl.read_text(encoding="utf-8")) and (
            "4.17.15" not in (work / "package-lock.json").read_text(encoding="utf-8")
            or not (work / "test" / "lodash.test.js").exists()
        )
        out = "# pass 2\n# fail 0\n" if ok else "# pass 1\n# fail 1\n"
        return ExecutionResult(ExecutionBackend.DOCKER, spec.argv, 0 if ok else 1, out, "", 0.1)


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


@pytest.fixture
def project(tmp_path: Path, git_env: None) -> LocalGitRepository:
    repo = LocalGitRepository.init(tmp_path / "calc")
    for rel, text in FILES.items():
        (repo.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo.root / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo.root, check=True)
    return repo


def config(fake: FakeLLM, **overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "ai__enabled": True, "ai__provider": "ollama", "ai__model": "local-model",
        "ai__base_url": f"{fake.url}/v1", "execution__docker__network": "bridge",
        "security__network": "allowlist",
        "security__egress_allowlist": ["localhost", "registry.npmjs.org"],
    }  # fmt: skip
    return make_config(Mode.MAINTAIN, **{**settings, **overrides})


async def test_maintain_plan_upgrade_and_next_item(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    sandbox = Sandbox()
    fake_llm.scripts["maintenance"] = [PLAN]
    fake_llm.scripts["developer"] = [LODASH_TEST, GOOD]
    request = MaintainRequest(issues=tuple(ISSUES), dependencies=tuple(DEPENDENCIES))
    registry, steps = build_maintain(request, executor=sandbox, deps_executor=sandbox)
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    first = await orch.execute(run_id)
    assert first.state is RunState.AWAITING_APPROVAL  # the plan needs a human
    assert fake_llm.agents_called() == ["maintenance"]
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert len(approved) == 1  # exactly the plan approval
    item = orch.task_output(run_id, "maintain.item")
    assert item is not None and item["item"]["id"] == "M2"  # p0 first
    assert item["next_item"] == "M1" and item["done"] == ["M2"]
    branch = item["branch"]
    assert branch.startswith("ai/maintain/plan-")
    changed = {c["path"] for c in item["changes"]}
    assert changed == {"package.json", "package-lock.json", "test/lodash.test.js"}
    lock = project.file_at(branch, "package-lock.json")
    assert "4.17.21" in lock and "4.17.15" not in lock  # written by the package manager
    assert project.file_at("main", "package-lock.json") == FILES["package-lock.json"]
    npm = [s for s in sandbox.specs if s.argv[0] == "npm" and "install" in s.argv]
    assert len(npm) == 1  # one upgrade, run by the package manager
    # Only package-manager steps may use the network; project tests never do.
    assert not any(s.network for s in sandbox.specs if s.argv[0] != "npm")
    body = item["pr_body"]
    assert "Задача сопровождения M2" in body and "test/lodash.test.js" in body
    prompt = json.dumps(fake_llm.requests[1]["body"])  # the developer saw the upgrade note
    assert "do not edit lock files" in prompt and "lodash 4.17.21" in prompt
    assert (orch.task_output(run_id, "maintain.verify") or {}).get("verified") is True

    # Next item of the same approved plan: no new plan, no new approval.
    nxt = MaintainRequest(item=item["next_item"], plan=item["maintenance_plan"],
                          plan_version=item["plan_version"], sources=item["sources"])  # fmt: skip
    registry, steps = build_maintain(nxt, executor=sandbox, deps_executor=sandbox,
                                     done=item["done"])  # fmt: skip
    assert "maintain.plan" not in {s.key for s in steps}
    orch2 = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    run2 = orch2.create_run(TaskPlanner().build(steps))
    status2, approved2 = await drive(orch2, run2)
    assert status2.state is RunState.COMPLETED, status2.error
    assert approved2 == []
    second = orch2.task_output(run2, "maintain.item")
    assert second is not None and second["item"]["id"] == "M1" and second["next_item"] is None
    assert "src/multiply.js" in project.list_files(second["branch"])


async def test_dependency_upgrade_needs_network_and_policy(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    request = MaintainRequest(dependencies=tuple(DEPENDENCIES), item="M2",
                              plan=PLAN, plan_version="m-x",
                              sources={"dependencies": DEPENDENCIES, "issues": ISSUES})  # fmt: skip
    registry, steps = build_maintain(request, executor=Sandbox(), deps_executor=Sandbox())
    offline = config(fake_llm, execution__docker__network="none")
    orch = make_orchestrator(project.root, list(registry.all()), config=offline)
    status = await orch.execute(orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED and "network is 'none'" in (status.error or "")
    denied = config(fake_llm, security__egress_allowlist=["localhost"])
    orch = make_orchestrator(project.root, list(registry.all()), config=denied)
    status = await orch.execute(orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED and "registry.npmjs.org" in (status.error or "")
    invented = [{"source": "D1", "to_version": "9.9.9"}]
    bad = {**PLAN, "items": [{**PLAN["items"][1], "upgrades": invented}]}
    request = MaintainRequest(item="M2", plan=bad, plan_version="m-y",
                              sources={"dependencies": DEPENDENCIES})  # fmt: skip
    registry, steps = build_maintain(request, executor=Sandbox(), deps_executor=Sandbox())
    orch = make_orchestrator(project.root, list(registry.all()), config=config(fake_llm))
    status = await orch.execute(orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED
    assert "not backed by a scanner-reported fix" in (status.error or "")


def test_maintain_cli(tmp_path: Path, project: LocalGitRepository, fake_llm: FakeLLM) -> None:
    # network none: the real baseline run skips the dependency install instead of trying it
    write_config(
        project.root / ".ai-coding.yml", config(fake_llm, execution__docker__network="none")
    )
    base = ["maintain", "-C", str(project.root), "--no-interactive"]
    assert runner.invoke(app, base).exit_code == ExitCode.USAGE  # nothing to maintain
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    assert runner.invoke(app, [*base, "--report", str(tmp_path / "bad.json")]).exit_code \
        == ExitCode.INVALID  # fmt: skip
    assert runner.invoke(app, [*base, "--report", str(tmp_path / "x.json")]).exit_code \
        == ExitCode.NOT_FOUND  # fmt: skip
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps(ISSUES), encoding="utf-8")
    dry = runner.invoke(app, [*base, "--issues-file", str(issues), "--dry-run"])
    assert dry.exit_code == 0 and "maintain.plan" in dry.output and "nothing changed" in dry.output
    fake_llm.scripts["maintenance"] = [{**PLAN, "items": PLAN["items"][:1]}]
    waiting = runner.invoke(app, [*base, "--issues-file", str(issues)])
    assert waiting.exit_code == ExitCode.PENDING, waiting.output
    assert "M1 [p1/bug] Add multiply()" in waiting.output
    assert "ai-coding maintain --resume" in waiting.output
    assert runner.invoke(app, [*base, "--continue", "nope"]).exit_code != 0


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(result: Any) -> str:
    """CLI output without colour codes (CI sets FORCE_COLOR, Rich adds ANSI codes)."""
    return _ANSI.sub("", result.output)


def test_maintain_cli_resume_complete_and_continue(
    tmp_path: Path, project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    """The whole CLI cycle: plan waits for approval (resume before approval keeps it
    waiting), approve, resume to a committed item, then --continue reports the plan done.
    Input errors are refused with the right exit codes."""
    write_config(
        project.root / ".ai-coding.yml", config(fake_llm, execution__docker__network="none")
    )
    base = ["maintain", "-C", str(project.root), "--no-interactive"]
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    broken = runner.invoke(app, [*base, "--report", str(tmp_path / "broken.json")])
    assert broken.exit_code == ExitCode.INVALID and "not valid JSON" in _plain(broken)
    (tmp_path / "shape.json").write_text('[{"body": "no title"}]', encoding="utf-8")
    shape = runner.invoke(app, [*base, "--issues-file", str(tmp_path / "shape.json")])
    assert shape.exit_code == ExitCode.INVALID and "JSON list of" in _plain(shape)

    issues = tmp_path / "issues.json"
    later = {"id": "#8", "title": "Dark mode", "body": "Some day."}
    issues.write_text(json.dumps([*ISSUES, later]), encoding="utf-8")
    deferred = [{"source": "#8", "reason": "later"}]
    fake_llm.scripts["maintenance"] = [{**PLAN, "items": PLAN["items"][:1], "deferred": deferred}]
    waiting = runner.invoke(app, [*base, "--issues-file", str(issues)])
    assert waiting.exit_code == ExitCode.PENDING, _plain(waiting)
    assert "deferred #8: later" in _plain(waiting)
    run_id = json.loads(
        runner.invoke(app, ["run", "list", "-C", str(project.root), "--json"]).output
    )[0]["id"]

    no_item = runner.invoke(app, [*base, "--continue", run_id])
    assert no_item.exit_code == ExitCode.NOT_FOUND and "no completed maintenance item" in (
        _plain(no_item)
    )
    still = runner.invoke(app, [*base, "--resume", run_id])
    assert still.exit_code == ExitCode.PENDING, _plain(still)  # nobody approved yet
    assert runner.invoke(app, ["run", "approve", run_id, "-C", str(project.root)]).exit_code == 0
    fake_llm.scripts["developer"] = [GOOD]
    done = runner.invoke(app, [*base, "--resume", run_id])
    assert done.exit_code == 0, _plain(done)
    assert "committed on" in _plain(done) and "next: ai-coding maintain" not in _plain(done)
    finished = runner.invoke(app, [*base, "--continue", run_id])
    assert finished.exit_code == ExitCode.USAGE and "every item of the plan is done" in (
        _plain(finished)
    )
