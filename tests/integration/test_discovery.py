"""Built-in discovery handlers and the `ai-coding run` CLI against a real git project."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config import validate_file
from ai_coding.domain.enums import ApprovalOperation, RunState
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator import Orchestrator, StepSpec, TaskPlanner
from ai_coding.orchestrator.handlers import builtin_registry, discovery_plan
from ai_coding.persistence import Database, upgrade_to_head
from ai_coding.persistence.models import Task

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def project(git_repo: LocalGitRepository) -> Path:
    root = git_repo.root
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=ghp_" + "x" * 36 + "\n", encoding="utf-8")
    result = runner.invoke(app, ["init", str(root), "--mode", "develop"])
    assert result.exit_code == 0, result.output
    return root


async def test_discovery_run_in_process(project: Path) -> None:
    config = validate_file(project / ".ai-coding.yml").config
    assert config is not None
    db = Database.for_project(project)
    upgrade_to_head(db)
    orch = Orchestrator(db=db, config=config, project_root=project, registry=builtin_registry())
    run_id = orch.create_run(TaskPlanner().build(discovery_plan()))
    status = await orch.execute(run_id)
    assert status.state is RunState.COMPLETED
    with db.session() as s:
        outputs = {t.key: t.output for t in s.query(Task)}
    assert outputs["discover.config"]["mode"] == "develop"  # type: ignore[index]
    git = outputs["discover.git"]
    assert git["is_repository"] and git["branch"] == "main" and git["on_protected_branch"]  # type: ignore[index]
    stacks = outputs["discover.stacks"]
    assert stacks["detected"] == ["python"] and stacks["evidence"]["python"] == "pyproject.toml"  # type: ignore[index]
    context = outputs["discover.context"]
    assert context["indexed_files"] >= 2 and context["excluded_sensitive_or_vendored"] >= 1  # type: ignore[index]
    report = json.loads(orch.artifacts.get(status.summary.artifacts[0]["sha256"]))
    assert set(report["sections"]) == {
        "discover.config",
        "discover.git",
        "discover.stacks",
        "discover.context",
    }
    assert "ghp_" not in json.dumps(report)
    # Local state never shows up as a change in the user's repository.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True, check=True
    ).stdout
    assert ".ai-coding/" not in porcelain
    orch.close()


def test_discovery_without_git(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path)])
    result = runner.invoke(app, ["run", "discover", "-C", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "COMPLETED"


def test_cli_run_lifecycle(project: Path) -> None:
    dry = runner.invoke(app, ["run", "discover", "-C", str(project), "--dry-run"])
    assert dry.exit_code == 0 and "nothing was executed" in dry.output
    dry_json = runner.invoke(app, ["run", "discover", "-C", str(project), "--dry-run", "--json"])
    assert [p["key"] for p in json.loads(dry_json.output)][-1] == "report.discovery"
    assert (
        json.loads(runner.invoke(app, ["run", "list", "-C", str(project), "--json"]).output) == []
    )

    result = runner.invoke(app, ["run", "discover", "-C", str(project), "--idempotency-key", "k1"])
    assert result.exit_code == 0, result.output
    assert "COMPLETED" in result.output and "discovery-report.json" in result.output
    runs = json.loads(runner.invoke(app, ["run", "list", "-C", str(project), "--json"]).output)
    assert len(runs) == 1
    run_id = runs[0]["id"]
    short = run_id[-12:]
    again = runner.invoke(
        app, ["run", "discover", "-C", str(project), "--idempotency-key", "k1", "--json"]
    )
    assert json.loads(again.output)["id"] == run_id  # idempotent re-submission

    status = runner.invoke(app, ["run", "status", short, "-C", str(project), "--json"])
    assert json.loads(status.output)["state"] == "COMPLETED"
    assert "COMPLETED" in runner.invoke(app, ["run", "status", short, "-C", str(project)]).output
    events = json.loads(
        runner.invoke(app, ["run", "events", short, "-C", str(project), "--json"]).output
    )
    assert [e["to"] for e in events][-1] == "COMPLETED"
    assert "VERIFYING" in runner.invoke(app, ["run", "events", short, "-C", str(project)]).output
    listing = runner.invoke(app, ["run", "list", "-C", str(project)])
    assert short in listing.output

    out = project / "audit.json"
    audit = runner.invoke(app, ["run", "audit", short, "-C", str(project), "-o", str(out)])
    assert audit.exit_code == 0
    trail = json.loads(out.read_text(encoding="utf-8"))
    assert trail[0]["action"] == "run.create" and all(e["run_id"] == run_id for e in trail)
    assert json.loads(runner.invoke(app, ["run", "audit", short, "-C", str(project)]).output)

    resume = runner.invoke(app, ["run", "resume", short, "-C", str(project)])
    assert resume.exit_code == ExitCode.INVALID and "cannot be resumed" in resume.output
    cancel = runner.invoke(app, ["run", "cancel", short, "-C", str(project), "--yes"])
    assert cancel.exit_code == ExitCode.INVALID
    dry_cancel = runner.invoke(app, ["run", "cancel", short, "-C", str(project), "--dry-run"])
    assert "would cancel" in dry_cancel.output
    no_pending = runner.invoke(app, ["run", "approve", short, "-C", str(project)])
    assert no_pending.exit_code == ExitCode.INVALID and "no pending approval" in no_pending.output
    unknown = runner.invoke(app, ["run", "status", "ffffffffffff", "-C", str(project)])
    assert unknown.exit_code == ExitCode.NOT_FOUND


def test_cli_requires_valid_config(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["run", "list", "-C", str(tmp_path)])
    assert missing.exit_code == ExitCode.NOT_FOUND
    (tmp_path / ".ai-coding.yml").write_text("mode: nope\n", encoding="utf-8")
    invalid = runner.invoke(app, ["run", "list", "-C", str(tmp_path)])
    assert invalid.exit_code == ExitCode.INVALID


def test_cli_approval_flow(project: Path) -> None:
    """Approve/reject through the CLI on a run waiting for approval (AC-10)."""
    reg = builtin_registry
    config = validate_file(project / ".ai-coding.yml").config
    assert config is not None
    db = Database.for_project(project)
    upgrade_to_head(db)
    orch = Orchestrator(db=db, config=config, project_root=project, registry=reg())
    plan = TaskPlanner().build(
        [StepSpec(key="git", capability="discovery.git", requires_approval=ApprovalOperation.MERGE)]
    )
    first = orch.create_run(plan, idempotency_key="a")
    second = orch.create_run(plan, idempotency_key="b")
    for rid in (first, second):
        assert asyncio.run(orch.execute(rid)).state is RunState.AWAITING_APPROVAL
    orch.close()

    waiting = runner.invoke(app, ["run", "status", str(first)[-12:], "-C", str(project)])
    assert "approval needed" in waiting.output
    approval_id = json.loads(
        runner.invoke(app, ["run", "status", str(first), "-C", str(project), "--json"]).output
    )["approvals"][0]["id"]
    bad = runner.invoke(app, ["run", "approve", str(first), "-C", str(project), "--approval", "zz"])
    assert bad.exit_code == ExitCode.NOT_FOUND
    ok = runner.invoke(
        app,
        ["run", "approve", str(first), "-C", str(project), "--approval", approval_id[-12:],
         "--note", "lgtm", "--resume", "--json"],
    )  # fmt: skip
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output.split("\n", 1)[1])["state"] == "COMPLETED"

    rejected = runner.invoke(app, ["run", "reject", str(second), "-C", str(project)])
    assert rejected.exit_code == 0 and "rejected" in rejected.output
    resumed = runner.invoke(app, ["run", "resume", str(second), "-C", str(project)])
    assert resumed.exit_code == ExitCode.INVALID and "CANCELLED" in resumed.output

    third_db = Database.for_project(project)
    orch3 = Orchestrator(db=third_db, config=config, project_root=project, registry=reg())
    third = orch3.create_run(plan, idempotency_key="c")
    orch3.close()
    declined = runner.invoke(app, ["run", "cancel", str(third), "-C", str(project)], input="n\n")
    assert declined.exit_code == ExitCode.ABORTED
    cancelled = runner.invoke(app, ["run", "cancel", str(third), "-C", str(project), "--yes"])
    assert cancelled.exit_code == 0 and "CANCELLED" in cancelled.output
    force = runner.invoke(
        app, ["run", "resume", str(third), "-C", str(project), "--force"], input="n\n"
    )
    assert force.exit_code == ExitCode.ABORTED
