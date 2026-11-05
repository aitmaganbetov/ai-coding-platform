"""``ai-coding audit --triage``: advisory Security Agent triage of the findings (S7)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from tests.fake_llm import Error, FakeLLM
from tests.integration.test_audit import AWS_KEY, _require_local, vulnerable  # noqa: F401

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def fake_llm() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


def enable_ai(root: Path, url: str) -> None:
    path = root / ".ai-coding.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["ai"] = {**(data.get("ai") or {}), "enabled": True, "provider": "ollama",
                  "model": "local-model", "base_url": f"{url}/v1"}  # fmt: skip
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def audit(root: Path, *extra: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    result = runner.invoke(app, ["audit", "-C", str(root), "-b", "local", "-t", "ruff",
                                 "--json", *extra])  # fmt: skip
    return result.exit_code, json.loads(result.output) if result.output.startswith("{") else {}


def test_triage_is_advisory_and_attached_to_findings(
    vulnerable: Path,  # noqa: F811
    fake_llm: FakeLLM,
) -> None:
    _require_local("ruff")
    no_ai = runner.invoke(app, ["audit", "-C", str(vulnerable), "-b", "local", "--triage"])
    assert no_ai.exit_code == ExitCode.USAGE and "needs `ai`" in no_ai.output
    enable_ai(vulnerable, fake_llm.url)
    code, plain = audit(vulnerable)
    assert code == ExitCode.INVALID and plain["triage"] is None
    ids = [f["fingerprint"][:12] for f in plain["findings"]]
    assessments = [{"finding_id": i, "verdict": "confirmed" if n == 0 else "false_positive",
                    "severity": "low", "evidence": "seen in code", "recommendation": "fix it"}
                   for n, i in enumerate(ids)]  # fmt: skip
    fake_llm.scripts["security"] = [
        {"summary": "Mostly noise.", "assessments": assessments, "additional_risks": []}
    ]
    code, triaged = audit(vulnerable, "--triage")
    assert code == ExitCode.INVALID  # the model's opinion never changes the gates
    assert triaged["verdict"] == plain["verdict"] and triaged["gates"] == plain["gates"]
    assert triaged["triage"]["status"] == "done" and triaged["triage"]["triaged"] == len(ids)
    verdicts = [f["triage"]["verdict"] for f in triaged["findings"]]
    assert verdicts.count("confirmed") == 1 and all(
        f["status"] == "new" for f in triaged["findings"]
    )
    prompt = json.dumps(fake_llm.requests[-1]["body"])
    assert "app/main.py" in prompt and "subprocess.run" in prompt  # the code was shown
    assert AWS_KEY not in prompt


def test_triage_failure_keeps_the_audit(vulnerable: Path, fake_llm: FakeLLM) -> None:  # noqa: F811
    _require_local("ruff")
    enable_ai(vulnerable, fake_llm.url)
    fake_llm.scripts["security"] = [Error(400, "model crashed")] * 9
    code, report = audit(vulnerable, "--triage")
    assert code == ExitCode.INVALID and report["verdict"] == "FAIL"
    assert report["triage"]["status"] == "failed" and "model crashed" in report["triage"]["reason"]
    assert report["integrity"]["unchanged"] is True


def test_external_model_pauses_the_audit_for_approval(
    vulnerable: Path,  # noqa: F811
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triage is advisory, but an approval requirement is never swallowed."""
    _require_local("ruff")
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-ant-api03-" + "q" * 40)
    path = vulnerable / ".ai-coding.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["ai"] = {"enabled": True, "provider": "anthropic", "model": "claude-test",
                  "base_url": fake_llm.url, "api_key_env": "ANTHROPIC_TEST_KEY"}  # fmt: skip
    data.setdefault("approvals", {})["required_for"] = [
        "external_llm",
        "delete",
        "deploy",
        "merge",
        "migration",
    ]
    data.setdefault("security", {}).update(network="allowlist", egress_allowlist=["localhost"])
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(app, ["audit", "-C", str(vulnerable), "-b", "local", "-t", "ruff",
                                 "--triage"])  # fmt: skip
    assert result.exit_code == ExitCode.PENDING, result.output
    assert "external_llm" in result.output and fake_llm.requests == []
    run_id = json.loads(runner.invoke(app, ["run", "list", "-C", str(vulnerable), "--json"])
                        .output)[0]["id"]  # fmt: skip
    generic = runner.invoke(app, ["run", "approve", str(run_id), "-C", str(vulnerable),
                                  "--resume"])  # fmt: skip
    assert generic.exit_code == ExitCode.USAGE and "ai-coding audit --resume" in generic.output
    resumed = runner.invoke(app, ["audit", "-C", str(vulnerable), "-b", "local",
                                  "--resume", str(run_id), "--json"])  # fmt: skip
    assert resumed.exit_code == ExitCode.INVALID, resumed.output  # completed, verdict FAIL
    assert json.loads(resumed.output)["triage"]["status"] == "failed"  # no script: HTTP 400
    (req,) = fake_llm.requests  # sent only after the human approved
    assert req["kind"] == "anthropic" and req["headers"]["x-api-key"].startswith("sk-ant-")
    assert "sk-ant-" not in json.dumps(req["body"])
    status = runner.invoke(app, ["run", "status", str(run_id), "-C", str(vulnerable), "--json"])
    assert json.loads(status.output)["state"] == "COMPLETED"
