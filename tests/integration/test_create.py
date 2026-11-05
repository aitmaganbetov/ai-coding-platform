"""CREATE end-to-end over HTTP against a fake LLM API (S5, FR-03, AC-01)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ai_coding.domain.enums import ApprovalOperation, Mode, RunState
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.github import GitHubClient, GitHubHost, TokenAuth
from ai_coding.orchestrator import Orchestrator, TaskPlanner
from ai_coding.orchestrator.errors import TransientError
from ai_coding.persistence.models import ModelUsage
from ai_coding.workflows import create as create_module
from ai_coding.workflows.create import CreateRequest, build_create, spec_version
from tests import agent_samples as samples
from tests.fake_github import FakeGitHub
from tests.fake_llm import Error, FakeLLM
from tests.helpers import make_config, make_orchestrator

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


@pytest.fixture
def project(tmp_path: Path, git_env: None) -> LocalGitRepository:
    repo = LocalGitRepository.init(tmp_path / "shortener")
    repo.create_root_commit("chore: initialize repository")
    return repo


def ollama_config(fake: FakeLLM, **overrides: Any) -> Any:
    return make_config(
        Mode.CREATE,
        ai__enabled=True,
        ai__provider="ollama",
        ai__model="local-model",
        ai__base_url=f"{fake.url}/v1",
        **overrides,
    )


class _BranchShas(dict[str, str]):
    def __init__(self, repo: LocalGitRepository) -> None:
        super().__init__()
        self.repo = repo

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self.repo.rev_parse(key) if self.repo.branch_exists(key) else default


async def drive(orch: Orchestrator, run_id: Any) -> Any:
    """Execute, approving every pending approval (the human in the loop)."""
    status = await orch.execute(run_id)
    approved: list[str] = []
    for _ in range(5):
        if status.state is not RunState.AWAITING_APPROVAL:
            break
        approved += [a.operation for a in orch.decide(run_id, approve=True)]
        status = await orch.resume(run_id)
    return status, approved


async def test_create_offline_with_local_model(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    registry, steps = build_create(CreateRequest(idea=samples.IDEA, project_name="Link Shortener"))
    config = ollama_config(fake_llm)
    orch = make_orchestrator(project.root, list(registry.all()), config=config)
    run_id = orch.create_run(TaskPlanner().build(steps), goal="create")
    first = await orch.execute(run_id)
    # The requirements version is shown to a human before any design work.
    assert first.state is RunState.AWAITING_APPROVAL
    assert [a["operation"] for a in first.approvals] == [ApprovalOperation.SPEC_APPROVAL.value]
    assert fake_llm.agents_called() == ["requirements"]
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert approved == ["spec_approval", "critical_file_change"]
    assert fake_llm.agents_called() == ["requirements", "architect", "planner"]
    # Local provider: no API key, structured output requested.
    first_req = fake_llm.requests[0]
    assert "authorization" not in first_req["headers"]
    assert first_req["body"]["response_format"]["type"] == "json_schema"

    tasks = {t["key"]: t for t in status.tasks}
    version = spec_version(samples.SPEC | {}, None)
    assert version.startswith("v1-")
    outputs = orch.audit_trail(run_id)
    assert outputs  # audit trail recorded

    # The skeleton is on the work branch only; main is untouched.
    assert project.current_branch() == "main"
    assert project.list_files("main") == []
    files = project.list_files("ai/bootstrap")
    assert {"README.md", "docs/requirements.md", "docs/architecture.md", "docs/backlog.md",
            "docs/adr/0001-fastapi.md", "src/link_shortener/links/__init__.py",
            ".github/workflows/ci.yml", ".ai-coding.yml"} <= set(files)  # fmt: skip
    readme = project.file_at("ai/bootstrap", "README.md")
    assert readme.startswith("# Link Shortener")
    assert "Версия требований: `v1-" in project.file_at("ai/bootstrap", "docs/requirements.md")
    assert tasks["create.verify"]["status"] == "SUCCEEDED"
    assert not (project.root / ".ai-coding" / "worktrees").exists() or not any(
        (project.root / ".ai-coding" / "worktrees").iterdir()
    )

    # The generated project passes its own tests.
    checkout = project.root.parent / "checkout"
    subprocess.run(["git", "clone", "-q", "-b", "ai/bootstrap", str(project.root), str(checkout)],
                   check=True)  # fmt: skip
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                         cwd=checkout, capture_output=True, text=True, check=False)  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr

    # Usage was accounted per agent call.
    with orch.db.session() as session:
        rows = session.query(ModelUsage).filter(ModelUsage.run_id == run_id).all()
    assert len(rows) == 3 and all(r.provider == "ollama" for r in rows)


async def test_retry_after_commit_does_not_commit_twice(
    project: LocalGitRepository, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = create_module._save
    failed: list[str] = []

    def flaky(ctx: Any, name: str, text: str, mime: str) -> str:
        if name == "bootstrap-manifest.json" and not failed:
            failed.append(name)  # the commit already happened when this fails
            raise TransientError("disk hiccup")
        return original(ctx, name, text, mime)

    monkeypatch.setattr(create_module, "_save", flaky)
    registry, steps = build_create(CreateRequest(idea=samples.IDEA))
    orch = make_orchestrator(project.root, list(registry.all()), config=ollama_config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status, _ = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert failed and len(project.find_commits_by_trailer("AI-Coding-Run", str(run_id))) == 1
    boot = next(t for t in status.tasks if t["key"] == "create.bootstrap")
    assert boot["attempts"] == 3  # approval pause, failure after the commit, success


async def test_existing_branch_is_never_overwritten(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    registry, steps = build_create(CreateRequest(idea=samples.IDEA))
    orch = make_orchestrator(project.root, list(registry.all()), config=ollama_config(fake_llm))
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.COMPLETED, status.error
    commit = project.rev_parse("ai/bootstrap")
    second, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps), goal="again"))
    assert second.state is RunState.FAILED
    assert "branch 'ai/bootstrap' already exists" in (second.error or "")
    assert project.rev_parse("ai/bootstrap") == commit


async def test_rejected_spec_stops_before_architecture(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    registry, steps = build_create(CreateRequest(idea=samples.IDEA))
    orch = make_orchestrator(project.root, list(registry.all()), config=ollama_config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    assert (await orch.execute(run_id)).state is RunState.AWAITING_APPROVAL
    orch.decide(run_id, approve=False, note="needs expiry")
    status = await orch.resume(run_id)
    assert status.state is RunState.CANCELLED
    assert fake_llm.agents_called() == ["requirements"]
    assert not project.branch_exists("ai/bootstrap")


async def test_revision_round_produces_next_version(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    previous = samples.spec()
    request = CreateRequest(idea=samples.IDEA, previous=previous, previous_version="v1-aaaaaaaa",
                            answers={"Q-01": "Ссылки живут 30 дней"})  # fmt: skip
    fake_llm.scripts["requirements"] = [samples.spec(open_questions=[])]
    registry, steps = build_create(request)
    orch = make_orchestrator(project.root, list(registry.all()), config=ollama_config(fake_llm))
    run_id = orch.create_run(TaskPlanner().build(steps))
    status = await orch.execute(run_id)
    req_task = next(t for t in status.tasks if t["key"] == "create.requirements")
    assert req_task["status"] == "SUCCEEDED"
    prompt = fake_llm.requests[0]["body"]["messages"][1]["content"]
    assert "Ссылки живут 30 дней" in prompt and "previous_specification" in prompt
    pending = status.approvals[0]
    assert "v2-" in pending["reason"]


async def test_fallback_after_primary_failure_and_repair(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    broken = samples.arch()
    broken["modules"][0]["depends_on"] = ["ghost"]
    fake_llm.scripts["requirements"] = [Error(503, "overloaded")]  # primary fails → fallback
    fake_llm.scripts["architect"] = [broken]  # repaired on the next round
    config = ollama_config(
        fake_llm,
        ai__fallback=[{"provider": "vllm", "model": "backup-model",
                       "base_url": f"{fake_llm.url}/v1"}],
    )  # fmt: skip
    registry, steps = build_create(CreateRequest(idea=samples.IDEA))
    orch = make_orchestrator(project.root, list(registry.all()), config=config)
    status, _ = await drive(orch, orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.COMPLETED, status.error
    models = [r["body"]["model"] for r in fake_llm.requests]
    assert models[:2] == ["local-model", "backup-model"]
    assert fake_llm.agents_called().count("architect") == 2
    repair = fake_llm.requests[3]["body"]["messages"][-1]["content"]
    assert "unknown module ghost" in repair


async def test_external_provider_needs_approval_and_key_stays_secret(
    project: LocalGitRepository, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "sk-ant-api03-" + "q" * 40
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", key)
    config = make_config(
        Mode.CREATE,
        ai__enabled=True, ai__provider="anthropic", ai__model="claude-test",
        ai__base_url=fake_llm.url, ai__api_key_env="ANTHROPIC_TEST_KEY",
        approvals__required_for=["external_llm", "delete", "migration", "merge", "deploy"],
        security__network="allowlist", security__egress_allowlist=["localhost"],
        ai__pricing={"claude-test": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}},
    )  # fmt: skip
    registry, steps = build_create(CreateRequest(idea=f"{samples.IDEA} token={key}"))
    orch = make_orchestrator(project.root, list(registry.all()), config=config)
    run_id = orch.create_run(TaskPlanner().build(steps))
    first = await orch.execute(run_id)
    assert first.state is RunState.AWAITING_APPROVAL
    assert first.approvals[0]["operation"] == "external_llm"
    assert fake_llm.requests == []  # nothing left the process before approval
    status, approved = await drive(orch, run_id)
    assert status.state is RunState.COMPLETED, status.error
    assert approved[0] == "external_llm"
    req = fake_llm.requests[0]
    assert req["kind"] == "anthropic" and req["headers"]["x-api-key"] == key
    assert key not in json.dumps(req["body"])  # redacted before sending
    with orch.db.session() as session:
        costs = [r.cost_usd for r in session.query(ModelUsage).filter(ModelUsage.run_id == run_id)]
    assert len(costs) == 3 and all(c > 0 for c in costs)  # priced from ai.pricing


async def test_create_with_pull_request(
    project: LocalGitRepository, fake_llm: FakeLLM, tmp_path: Path
) -> None:
    gh = FakeGitHub().start()
    try:
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run(["git", "remote", "add", "origin", bare.as_uri()], cwd=project.root,
                       check=True)  # fmt: skip
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=project.root, check=True)
        host = GitHubHost(GitHubClient(TokenAuth(gh.token), api_url=gh.url), gh.repo)
        registry, steps = build_create(
            CreateRequest(idea=samples.IDEA),
            pull_request=(host, lambda: None, "localhost", "Bootstrap project"),
        )
        assert {s.key for s in steps} >= {"git.push", "github.pr", "github.pr.verify"}
        config = ollama_config(fake_llm, security__network="allowlist",
                               security__egress_allowlist=["localhost"])  # fmt: skip
        orch = make_orchestrator(project.root, list(registry.all()), config=config)
        run_id = orch.create_run(TaskPlanner().build(steps))
        gh.head_shas = _BranchShas(project)  # the fake PR reports the pushed head
        status, approved = await drive(orch, run_id)
        assert status.state is RunState.COMPLETED, status.error
        assert approved == ["pr_create", "spec_approval", "critical_file_change"]
        pr = gh.pulls[1]
        assert pr["head"]["ref"] == "ai/bootstrap" and pr["base"]["ref"] == "main"
        assert pr["title"] == "Bootstrap project" and pr["auto_merge"] is None
    finally:
        gh.stop()


async def test_missing_api_key_fails_with_a_clear_message(
    project: LocalGitRepository, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_SUCH_KEY_FOR_TEST", raising=False)
    config = make_config(
        Mode.CREATE, ai__enabled=True, ai__provider="openai", ai__model="gpt-test",
        ai__base_url=f"{fake_llm.url}/v1", ai__api_key_env="NO_SUCH_KEY_FOR_TEST",
        security__network="allowlist", security__egress_allowlist=["localhost"],
    )  # fmt: skip
    registry, steps = build_create(CreateRequest(idea=samples.IDEA))
    orch = make_orchestrator(project.root, list(registry.all()), config=config)
    status = await orch.execute(orch.create_run(TaskPlanner().build(steps)))
    assert status.state is RunState.FAILED
    assert "$NO_SUCH_KEY_FOR_TEST is not set" in (status.error or "")
    assert fake_llm.requests == []


async def test_budget_exhaustion_pauses_the_run(
    project: LocalGitRepository, fake_llm: FakeLLM
) -> None:
    config = ollama_config(fake_llm, budgets__max_tokens_per_run=100)
    registry, steps = build_create(CreateRequest(idea=samples.IDEA))
    orch = make_orchestrator(project.root, list(registry.all()), config=config)
    run_id = orch.create_run(TaskPlanner().build(steps))
    status = await orch.execute(run_id)
    assert status.state is RunState.PAUSED
    assert any("budget" in json.dumps(e).lower() for e in orch.events(run_id))
    assert fake_llm.requests == []  # the pre-check stops the call before it is sent
