"""CLI tests via Typer's runner (in-process)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding import __version__
from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config import validate_file
from ai_coding.git import LocalGitRepository

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output
    assert "config" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"ai-coding {__version__}"


def test_config_help() -> None:
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    assert "validate" in result.output and "schema" in result.output


# ------------------------------------------------------------------- init --
def test_init_creates_valid_config(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    result = runner.invoke(app, ["init", str(tmp_path), "--name", "svc", "--mode", "develop"])
    assert result.exit_code == 0, result.output
    cfg_path = tmp_path / ".ai-coding.yml"
    report = validate_file(cfg_path)
    assert report.ok and report.config is not None
    assert report.config.project.name == "svc"
    assert [s.value for s in report.config.stacks] == ["go"]
    assert "go (go.mod)" in result.output
    assert "not a repository" in result.output


def test_init_uses_directory_name(tmp_path: Path) -> None:
    project = tmp_path / "My Service"
    project.mkdir()
    assert runner.invoke(app, ["init", str(project)]).exit_code == 0
    cfg = validate_file(project / ".ai-coding.yml").config
    assert cfg is not None and cfg.project.name == "My-Service"


def test_init_refuses_overwrite(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == ExitCode.INVALID
    assert "already exists" in result.output


def test_init_force_requires_confirmation(tmp_path: Path) -> None:
    target = tmp_path / ".ai-coding.yml"
    target.write_text("original", encoding="utf-8")
    declined = runner.invoke(app, ["init", str(tmp_path), "--force"], input="n\n")
    assert declined.exit_code == ExitCode.ABORTED
    assert target.read_text(encoding="utf-8") == "original"

    accepted = runner.invoke(app, ["init", str(tmp_path), "--force"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert (tmp_path / ".ai-coding.yml.bak").read_text(encoding="utf-8") == "original"
    assert validate_file(target).ok


def test_init_force_yes(tmp_path: Path) -> None:
    (tmp_path / ".ai-coding.yml").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["init", str(tmp_path), "--force", "--yes"])
    assert result.exit_code == 0


def test_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--dry-run", "--stack", "rust"])
    assert result.exit_code == 0
    assert 'schema_version: "2.1"' in result.output
    assert "stacks: [rust]" in result.output
    assert not (tmp_path / ".ai-coding.yml").exists()


def test_init_stack_override_and_no_detect(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("", encoding="utf-8")
    result = runner.invoke(
        app, ["init", str(tmp_path), "--dry-run", "-s", "python", "-s", "python", "-s", "go"]
    )
    assert "stacks: [python, go]" in result.output
    result = runner.invoke(app, ["init", str(tmp_path), "--no-detect"])
    assert "detection disabled" in result.output


def test_init_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path / "missing")])
    assert result.exit_code == ExitCode.NOT_FOUND


def test_init_invalid_mode(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--mode", "deploy"])
    assert result.exit_code != 0


@pytest.mark.integration
def test_init_reports_git_state(tmp_path: Path, git_repo: object) -> None:
    assert isinstance(git_repo, LocalGitRepository)
    subprocess.run(["git", "branch", "-m", "main", "trunk"], cwd=git_repo.root, check=True)
    result = runner.invoke(app, ["init", str(git_repo.root)])
    assert result.exit_code == 0, result.output
    assert "on trunk (clean)" in result.output
    cfg = validate_file(git_repo.root / ".ai-coding.yml").config
    assert cfg is not None and cfg.project.default_branch == "trunk"


# -------------------------------------------------------- config validate --
def test_validate_ok(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path)])
    result = runner.invoke(app, ["config", "validate", str(tmp_path)])
    assert result.exit_code == 0
    assert "valid" in result.output


def test_validate_reports_issues(fixtures_dir: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text('schema_version: "2.1"\nproject: {name: x}\nmode: nope\n', encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", str(bad)])
    assert result.exit_code == ExitCode.INVALID
    assert "[schema] mode" in result.output


def test_validate_json_output(tmp_path: Path) -> None:
    secret = "ghp_" + "s" * 36
    bad = tmp_path / ".ai-coding.yml"
    bad.write_text(
        f'schema_version: "2.1"\nproject: {{name: x}}\nai: {{token: {secret}}}\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["config", "validate", str(tmp_path), "--format", "json"])
    assert result.exit_code == ExitCode.INVALID
    payload = json.loads(result.output)
    assert payload["valid"] is False
    assert any(i["kind"] == "secret" for i in payload["issues"])
    assert secret not in result.output


def test_validate_not_found(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "validate", str(tmp_path)])
    assert result.exit_code == ExitCode.NOT_FOUND
    result = runner.invoke(app, ["config", "validate", str(tmp_path / "x.yml"), "-f", "json"])
    assert result.exit_code == ExitCode.NOT_FOUND
    assert json.loads(result.output)["valid"] is False


def test_config_schema_stdout_and_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["$schema"].endswith("2020-12/schema")
    out = tmp_path / "s.json"
    assert runner.invoke(app, ["config", "schema", "-o", str(out)]).exit_code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["title"]


@pytest.mark.integration
def test_console_script_entry_point(tmp_path: Path) -> None:
    """The installed ``ai-coding`` executable works as a real process."""
    exe = shutil.which("ai-coding")
    if exe is None:
        pytest.skip("ai-coding console script is not installed in this environment")
    proc = subprocess.run([exe, "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "init" in proc.stdout
    proc = subprocess.run([exe, "init", str(tmp_path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    proc = subprocess.run(
        [exe, "config", "validate", str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_python_dash_m(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ai_coding", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert __version__ in proc.stdout
