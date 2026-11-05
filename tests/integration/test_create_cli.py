"""``ai-coding create`` and ``ai-coding agents list`` (S5, FR-03, AC-01, B-406, B-421)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.git import LocalGitRepository
from tests import agent_samples as samples
from tests.fake_llm import FakeLLM
from tests.helpers import make_config

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


@pytest.fixture
def config_file(tmp_path: Path, fake_llm: FakeLLM) -> Path:
    config = make_config(ai__enabled=True, ai__provider="ollama", ai__model="local-model",
                         ai__base_url=f"{fake_llm.url}/v1")  # fmt: skip
    return write_config(tmp_path / "platform.yml", config)


def write_config(path: Path, config: Any) -> Path:
    data = config.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def last_run(dest: Path) -> str:
    out = runner.invoke(app, ["run", "list", "-C", str(dest), "--json"]).output
    return str(json.loads(out)[0]["id"])


def test_non_interactive_flow(tmp_path: Path, config_file: Path, git_env: None) -> None:
    dest = tmp_path / "shortener"
    base = ["--dest", str(dest), "--config", str(config_file), "--no-interactive"]
    first = runner.invoke(app, ["create", samples.IDEA, *base, "--name", "Link Shortener"])
    assert first.exit_code == ExitCode.PENDING, first.output
    assert "Требования v1-" in first.output and "FR-01" in first.output and "Q-01" in first.output
    assert "ai-coding run approve" in first.output and "--revise" in first.output
    run_id = last_run(dest)
    # The generic resume refuses a CREATE run instead of failing it.
    generic = runner.invoke(app, ["run", "resume", run_id, "-C", str(dest)])
    assert generic.exit_code == ExitCode.USAGE and "create --resume" in generic.output
    assert runner.invoke(app, ["run", "approve", run_id, "-C", str(dest)]).exit_code == 0
    second = runner.invoke(app, ["create", "--resume", run_id, *base])
    assert second.exit_code == ExitCode.PENDING, second.output
    assert "critical_file_change" in second.output and ".github/workflows/ci.yml" in second.output
    assert runner.invoke(app, ["run", "approve", run_id, "-C", str(dest)]).exit_code == 0
    done = runner.invoke(app, ["create", "--resume", run_id, *base])
    assert done.exit_code == 0, done.output
    assert "project skeleton committed" in done.output and "ai/bootstrap" in done.output
    repo = LocalGitRepository.open(dest)
    assert "README.md" in repo.list_files("ai/bootstrap") and repo.list_files("main") == []


def test_interactive_approve_and_revise(
    tmp_path: Path, config_file: Path, fake_llm: FakeLLM, git_env: None
) -> None:
    fake_llm.scripts["requirements"] = [samples.spec(), samples.spec(open_questions=[])]
    dest = tmp_path / "svc"
    answers = "revise\nСсылки живут 30 дней\nДобавить QR-код\napprove\napprove\n"
    result = runner.invoke(
        app, ["create", samples.IDEA, "--dest", str(dest), "--config", str(config_file),
              "--interactive"], input=answers,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "revising requirements v1-" in result.output and "Требования v2-" in result.output
    assert fake_llm.agents_called() == ["requirements", "requirements", "architect", "planner"]
    revision_prompt = fake_llm.requests[1]["body"]["messages"][1]["content"]
    assert "Ссылки живут 30 дней" in revision_prompt and "Добавить QR-код" in revision_prompt
    runs = json.loads(runner.invoke(app, ["run", "list", "-C", str(dest), "--json"]).output)
    assert sorted(r["state"] for r in runs) == ["CANCELLED", "COMPLETED"]
    readme = LocalGitRepository.open(dest).file_at("ai/bootstrap", "docs/requirements.md")
    assert "Версия требований: `v2-" in readme


def test_revise_from_file(
    tmp_path: Path, config_file: Path, fake_llm: FakeLLM, git_env: None
) -> None:
    dest = tmp_path / "svc"
    base = ["--dest", str(dest), "--config", str(config_file), "--no-interactive"]
    assert runner.invoke(app, ["create", samples.IDEA, *base]).exit_code == ExitCode.PENDING
    first = last_run(dest)
    answers = tmp_path / "answers.yml"
    answers.write_text("Q-01: Бессрочно\n", encoding="utf-8")
    result = runner.invoke(app, ["create", "--revise", first, "--answers", str(answers), *base])
    assert result.exit_code == ExitCode.PENDING, result.output
    assert "Требования v2-" in result.output
    prompt = fake_llm.requests[1]["body"]["messages"][1]["content"]
    assert "Бессрочно" in prompt and samples.IDEA in prompt


def test_usage_errors_and_dry_run(
    tmp_path: Path, config_file: Path, git_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "x"
    base = ["--dest", str(dest), "--config", str(config_file), "--no-interactive"]
    assert runner.invoke(app, ["create", *base]).exit_code == ExitCode.USAGE
    both = runner.invoke(app, ["create", "--resume", "a", "--revise", "b", *base])
    assert both.exit_code == ExitCode.USAGE
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "notes.txt").write_text("x", encoding="utf-8")
    res = runner.invoke(app, ["create", "idea", "--dest", str(dirty), "--config", str(config_file)])
    assert res.exit_code == ExitCode.USAGE and "not empty" in res.output
    dry = runner.invoke(app, ["create", "idea", *base, "--dry-run"])
    assert dry.exit_code == 0 and "create.bootstrap: agent.bootstrap" in dry.output
    assert not dest.exists()
    monkeypatch.chdir(tmp_path)
    missing = runner.invoke(app, ["create", "idea", "--dest", str(dest)])
    assert missing.exit_code == ExitCode.NOT_FOUND and "--config" in missing.output
    bad = tmp_path / "bad.yml"
    bad.write_text("ai: [1]\n", encoding="utf-8")
    invalid = runner.invoke(app, ["create", "idea", "--dest", str(dest), "--config", str(bad)])
    assert invalid.exit_code == ExitCode.INVALID


def test_create_without_ai_fails_clearly(tmp_path: Path, git_env: None) -> None:
    cfg = write_config(tmp_path / "plain.yml", make_config())
    dest = tmp_path / "p"
    result = runner.invoke(app, ["create", "idea", "--dest", str(dest), "--config", str(cfg),
                                 "--no-interactive"])  # fmt: skip
    assert result.exit_code == ExitCode.INVALID
    assert "AI is disabled" in result.output


def test_agents_list(tmp_path: Path, config_file: Path) -> None:
    table = runner.invoke(app, ["agents", "list"])
    assert table.exit_code == 0
    for name in ("requirements", "bootstrap", "reviewer", "maintenance"):
        assert name in table.output
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".ai-coding.yml").write_text(
        config_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    rows = json.loads(runner.invoke(app, ["agents", "list", "-C", str(project), "--json"]).output)
    by_name = {r["name"]: r for r in rows}
    assert len(rows) == 12
    assert by_name["requirements"]["status"] == "implemented"
    assert by_name["requirements"]["models"] == ["ollama/local-model"]
    assert by_name["bootstrap"]["requires_llm"] is False and by_name["bootstrap"]["models"] == []
    assert by_name["developer"]["status"] == "planned (S6)"
    assert by_name["planner"]["output_schema"].endswith("planner.output.schema.json")
