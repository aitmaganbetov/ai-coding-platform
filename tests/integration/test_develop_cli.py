"""``ai-coding develop | modernize | review`` (S6, B-506, B-507)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.git import LocalGitRepository
from tests.fake_github import FakeGitHub
from tests.fake_llm import FakeLLM
from tests.helpers import make_config
from tests.integration.test_develop import GOOD, ISSUE, PROJECT, dev_output
from tests.integration.test_modernize import PLAN, STEP1, STEP2

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


def make_repo(root: Path, fake: FakeLLM, **overrides: Any) -> LocalGitRepository:
    repo = LocalGitRepository.init(root)
    for rel, text in PROJECT.items():
        (repo.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo.root / rel).write_text(text, encoding="utf-8")
    settings = {
        "ai__enabled": True,
        "ai__provider": "ollama",
        "ai__model": "local-model",
        "ai__base_url": f"{fake.url}/v1",
        **overrides,
    }
    cfg = make_config(**settings)
    (repo.root / ".ai-coding.yml").write_text(
        yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo.root, check=True)
    return repo


@pytest.fixture
def repo(tmp_path: Path, fake_llm: FakeLLM, git_env: None) -> LocalGitRepository:
    return make_repo(tmp_path / "calc", fake_llm)


def last_run(root: Path) -> str:
    out = runner.invoke(app, ["run", "list", "-C", str(root), "--json"]).output
    return str(json.loads(out)[0]["id"])


# ----------------------------------------------------------------- develop --
def test_develop_task(repo: LocalGitRepository, fake_llm: FakeLLM) -> None:
    fake_llm.scripts["developer"] = [GOOD]
    args = [
        "develop",
        "Add multiply function\nWe need multiply(a, b).",
        "-C",
        str(repo.root),
        "--no-interactive",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "committed on ai/add-multiply-function" in result.output
    assert "tests:" in result.output and "AI review (advisory): approve" in result.output
    assert "src/multiply.js" in repo.list_files("ai/add-multiply-function")
    prompt = fake_llm.requests[0]["body"]["messages"][1]["content"]
    assert "We need multiply(a, b)." in prompt


def test_develop_approval_resume_and_errors(repo: LocalGitRepository, fake_llm: FakeLLM) -> None:
    fake_llm.scripts["developer"] = [
        dev_output(
            "a * b",
            extra=[
                {
                    "path": ".github/workflows/ci.yml",
                    "action": "create",
                    "content": "on: push\n",
                    "reason": "ci",
                }
            ],
        )
    ]
    base = ["-C", str(repo.root), "--no-interactive"]
    waiting = runner.invoke(app, ["develop", "Add multiply", *base])
    assert waiting.exit_code == ExitCode.PENDING, waiting.output
    assert "approval needed: critical_file_change" in waiting.output
    assert "develop --resume" in waiting.output
    run_id = last_run(repo.root)
    assert runner.invoke(app, ["run", "approve", run_id, "-C", str(repo.root)]).exit_code == 0
    done = runner.invoke(app, ["develop", "--resume", run_id, *base])
    assert done.exit_code == 0, done.output
    assert ".github/workflows/ci.yml" in repo.list_files("ai/add-multiply")
    assert runner.invoke(app, ["develop", *base]).exit_code == ExitCode.USAGE
    dry = runner.invoke(app, ["develop", "Do x", *base, "--dry-run"])
    assert dry.exit_code == 0 and "develop.implement" in dry.output and "ai/do-x" in dry.output


def test_develop_issue_with_pr_interactive(
    repo: LocalGitRepository, fake_llm: FakeLLM, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", bare.as_uri()], cwd=repo.root, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo.root, check=True)
    cfg = yaml.safe_load((repo.root / ".ai-coding.yml").read_text(encoding="utf-8"))
    cfg["security"] = {"network": "allowlist", "egress_allowlist": ["localhost"]}
    (repo.root / ".ai-coding.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    fake_github.issues[7] = ISSUE

    class Shas(dict[str, str]):
        def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
            return repo.rev_parse(key) if repo.branch_exists(key) else default

    fake_github.head_shas = Shas()
    fake_llm.scripts["developer"] = [GOOD]
    result = runner.invoke(
        app,
        ["develop", "--issue", "7", "-C", str(repo.root), "--pr", "--interactive"],
        input="approve\n",
    )
    assert result.exit_code == 0, result.output
    assert "approval needed: pr_create" in result.output and "pull request:" in result.output
    pr = fake_github.pulls[1]
    assert pr["head"]["ref"] == "ai/7-add-multiply-function" and "Closes #7" in pr["body"]


# --------------------------------------------------------------- modernize --
def test_modernize_plan_continue_rollback(repo: LocalGitRepository, fake_llm: FakeLLM) -> None:
    fake_llm.scripts["migration"] = [PLAN]
    fake_llm.scripts["developer"] = [STEP1, STEP2]
    base = ["-C", str(repo.root)]
    first = runner.invoke(
        app, ["modernize", PLAN["goal"], *base, "--interactive"], input="approve\n"
    )
    assert first.exit_code == 0, first.output
    assert "Plan p1-" in first.output and "M-02 [low] Document style" in first.output
    assert "next: ai-coding modernize --continue" in first.output
    run1 = last_run(repo.root)
    second = runner.invoke(app, ["modernize", "--continue", run1, *base, "--no-interactive"])
    assert second.exit_code == 0, second.output
    run2 = last_run(repo.root)
    assert "README.md" in repo.list_files("ai/modernize-convert-functions-to-arrow-syntax-m-02")
    back = runner.invoke(app, ["modernize", "--rollback", run2, *base, "--no-interactive"])
    assert back.exit_code == 0, back.output
    assert "not merged into main: close its PR and delete branch" in back.output
    assert runner.invoke(app, ["modernize", *base]).exit_code == ExitCode.USAGE
    nothing = runner.invoke(app, ["modernize", "--continue", run2, *base, "--no-interactive"])
    assert nothing.exit_code == ExitCode.USAGE and "no further steps" in nothing.output


# ------------------------------------------------------------------ review --
def test_review_comment_and_check(
    repo: LocalGitRepository, fake_llm: FakeLLM, fake_github: FakeGitHub
) -> None:
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo.root, check=True)
    (repo.root / "src/sum.js").write_text("export const sum = (a, b) => a - b;\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "bug"], cwd=repo.root, check=True)
    finding = {
        "summary": "Wrong operator",
        "verdict": "request_changes",
        "findings": [
            {
                "file": "src/sum.js",
                "line": 1,
                "severity": "high",
                "category": "bug",
                "message": "subtracts instead of adding",
                "suggestion": "use +",
            }
        ],
    }
    fake_llm.scripts["reviewer"] = [finding]
    args = [
        "review",
        "--base",
        "main",
        "--head",
        "feature",
        "-C",
        str(repo.root),
        "--pr",
        "3",
        "--comment",
        "--check",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output  # advisory: neutral, not failure
    assert "subtracts instead of adding" in result.output
    comment = next(iter(fake_github.comments.values()))
    assert comment["body"].startswith("<!-- ai-coding:ai-review -->")
    check = next(iter(fake_github.check_runs.values()))
    assert check["conclusion"] == "neutral" and check["name"] == "ai-coding / ai-review"
    assert check["output"]["annotations"][0]["path"] == "src/sum.js"
    diff = fake_llm.requests[0]["body"]["messages"][1]["content"]
    assert "-  return a + b;" in diff and "a - b" in diff


def test_review_policy_blocking_and_skip(tmp_path: Path, fake_llm: FakeLLM, git_env: None) -> None:
    repo = make_repo(tmp_path / "p", fake_llm, quality_gates__review__ai_review_blocking=True)
    subprocess.run(["git", "checkout", "-qb", "f"], cwd=repo.root, check=True)
    (repo.root / "src/sum.js").write_text("// x\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "x"], cwd=repo.root, check=True)
    fake_llm.scripts["reviewer"] = [
        {
            "summary": "s",
            "verdict": "request_changes",
            "findings": [
                {
                    "file": "src/sum.js",
                    "line": 1,
                    "severity": "critical",
                    "category": "bug",
                    "message": "m",
                }
            ],
        }
    ]
    out = tmp_path / "review.json"
    blocked = runner.invoke(
        app,
        ["review", "--base", "main", "--head", "f", "-C", str(repo.root), "--json", "-o", str(out)],
    )
    assert blocked.exit_code == ExitCode.INVALID
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "request_changes"
    nothing = runner.invoke(app, ["review", "--base", "f", "--head", "f", "-C", str(repo.root)])
    assert nothing.exit_code == 0 and "No changes to review" in nothing.output
    plain = make_repo(tmp_path / "q", fake_llm, ai__enabled=False, ai__provider="none")
    skipped = runner.invoke(
        app, ["review", "--base", "main", "-C", str(plain.root), "--skip-if-disabled"]
    )
    assert skipped.exit_code == 0 and "skipped" in skipped.output
    usage = runner.invoke(app, ["review", "--base", "main", "-C", str(repo.root), "--comment"])
    assert usage.exit_code == ExitCode.USAGE
