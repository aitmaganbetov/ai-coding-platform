"""``ai-coding agents run``: any agent as a one-step orchestrated run (S7)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from tests.fake_llm import Error, FakeLLM
from tests.helpers import make_config
from tests.integration.test_create_cli import write_config
from tests.unit.test_agents_s7 import DEVOPS_INPUTS, DEVOPS_OUT, PERF_INPUTS, QA_INPUTS, QA_OUT

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


@pytest.fixture
def project(tmp_path: Path, fake_llm: FakeLLM) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    config = make_config(ai__enabled=True, ai__provider="ollama", ai__model="local-model",
                         ai__base_url=f"{fake_llm.url}/v1")  # fmt: skip
    write_config(root / ".ai-coding.yml", config)
    return root


def inputs(tmp_path: Path, data: object) -> str:
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_agent_run_outputs_validated_result(
    tmp_path: Path, project: Path, fake_llm: FakeLLM
) -> None:
    fake_llm.scripts["qa"] = [QA_OUT]
    out = tmp_path / "qa.json"
    result = runner.invoke(app, ["agents", "run", "qa", "-i", inputs(tmp_path, QA_INPUTS),
                                 "-C", str(project), "-o", str(out)])  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [c["id"] for c in data["test_cases"]] == ["TC-1", "TC-2"]
    assert fake_llm.agents_called() == ["qa"]
    prompt = fake_llm.requests[0]["body"]["messages"][1]["content"]
    assert '<input name="acceptance_criteria">' in prompt


def test_agent_run_repairs_then_succeeds(tmp_path: Path, project: Path, fake_llm: FakeLLM) -> None:
    unsafe = {**DEVOPS_OUT, "pipeline": [{**s, "manual_approval": False}
                                         for s in DEVOPS_OUT["pipeline"]]}  # fmt: skip
    fake_llm.scripts["devops"] = [unsafe, DEVOPS_OUT]
    result = runner.invoke(app, ["agents", "run", "devops", "-i",
                                 inputs(tmp_path, DEVOPS_INPUTS), "-C", str(project)])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["pipeline"][2]["manual_approval"] is True
    repair = fake_llm.requests[1]["body"]["messages"][-1]["content"]
    assert "deploys to production without manual approval" in repair


def test_agent_run_failure_and_usage_errors(
    tmp_path: Path, project: Path, fake_llm: FakeLLM
) -> None:
    fake_llm.scripts["performance"] = [Error(400, "bad request")] * 3
    failed = runner.invoke(app, ["agents", "run", "performance", "-i",
                                 inputs(tmp_path, PERF_INPUTS), "-C", str(project)])  # fmt: skip
    assert failed.exit_code == ExitCode.INVALID and "failed" in failed.output.lower()
    base = ["-C", str(project)]
    unknown = runner.invoke(app, ["agents", "run", "nope", "-i", inputs(tmp_path, {}), *base])
    assert unknown.exit_code == ExitCode.USAGE and "unknown agent" in unknown.output
    missing = runner.invoke(app, ["agents", "run", "qa", "-i", str(tmp_path / "x.json"), *base])
    assert missing.exit_code == ExitCode.NOT_FOUND
    (tmp_path / "bad.json").write_text("[1]", encoding="utf-8")
    not_obj = runner.invoke(app, ["agents", "run", "qa", "-i", str(tmp_path / "bad.json"), *base])
    assert not_obj.exit_code == ExitCode.INVALID
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    broken = runner.invoke(app, ["agents", "run", "qa", "-i", str(tmp_path / "bad.json"), *base])
    assert broken.exit_code == ExitCode.INVALID
