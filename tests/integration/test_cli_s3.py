"""CLI: project add (FR-01) and tools list/pull."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.git import LocalGitRepository

pytestmark = pytest.mark.integration
runner = CliRunner()


def test_project_add_local(git_repo: LocalGitRepository) -> None:
    (git_repo.root / "go.mod").write_text("module x\n", encoding="utf-8")
    result = runner.invoke(app, ["project", "add", str(git_repo.root)])
    assert result.exit_code == 0, result.output
    assert "git: branch main" in result.output and "go (go.mod)" in result.output
    assert "ai-coding init" in result.output
    runner.invoke(app, ["init", str(git_repo.root)])
    again = runner.invoke(app, ["project", "add", str(git_repo.root)])
    assert "next: ai-coding audit" in again.output


def test_project_add_errors(tmp_path: Path) -> None:
    assert (
        runner.invoke(app, ["project", "add", str(tmp_path / "missing")]).exit_code
        == ExitCode.NOT_FOUND
    )
    plain = runner.invoke(app, ["project", "add", str(tmp_path)])
    assert plain.exit_code == 0 and "not a repository" in plain.output
    insecure = runner.invoke(app, ["project", "add", "http://example.com/r.git"])
    assert insecure.exit_code == ExitCode.USAGE and "unsupported repository URL" in insecure.output
    unreachable = runner.invoke(
        app, ["project", "add", "https://127.0.0.1:9/none.git", "--dest", str(tmp_path / "c")]
    )
    assert unreachable.exit_code == ExitCode.INVALID and "clone failed" in unreachable.output


def test_tools_list_and_pull_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ai_coding.cli.commands.tools.docker_available", lambda: False)
    listing = runner.invoke(app, ["tools", "list"])
    assert listing.exit_code == 0
    for tool in ("gitleaks", "ruff", "semgrep"):
        assert tool in listing.output
    assert "docker unavailable" in listing.output
    assert runner.invoke(app, ["tools", "pull"]).exit_code == ExitCode.NOT_FOUND


def test_tools_pull_unknown_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ai_coding.cli.commands.tools.docker_available", lambda: True)
    assert runner.invoke(app, ["tools", "pull", "-t", "nope"]).exit_code == ExitCode.USAGE
