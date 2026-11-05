"""AC-05: connect a local model and run an audit without external network requests.

The audit runs gitleaks and Semgrep in the Docker sandbox and the Security Agent triage
on a local, OpenAI-compatible model server (Ollama API) on this host, under
``security.network: local-only``. Every connection the platform opens is recorded:
only loopback is allowed, and every sandbox container runs with ``--network none``.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.executors.docker import DockerSandboxExecutor
from ai_coding.tools.gitleaks import GITLEAKS_IMAGE
from ai_coding.tools.semgrep import SEMGREP_IMAGE
from tests.fake_llm import FakeLLM
from tests.integration.conftest import require_images
from tests.integration.test_audit import AWS_KEY, vulnerable  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.docker]
runner = CliRunner()


@pytest.fixture
def local_model() -> Iterator[FakeLLM]:
    fake = FakeLLM().start()
    yield fake
    fake.stop()


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every outbound connection opened by this process."""
    seen: list[Any] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> Any:
        seen.append(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        seen.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return seen


@pytest.fixture
def sandbox_networks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ``--network`` of every sandbox container started."""
    networks: list[str] = []
    real = DockerSandboxExecutor.build_argv

    def build_argv(self: DockerSandboxExecutor, spec: Any, name: str) -> list[str]:
        argv = real(self, spec, name)
        networks.append(argv[argv.index("--network") + 1])
        return argv

    monkeypatch.setattr(DockerSandboxExecutor, "build_argv", build_argv)
    return networks


def _loopback(address: Any) -> bool:
    if isinstance(address, str | bytes):  # AF_UNIX (e.g. a local socket)
        return True
    host = address[0]
    return host == "localhost" or ipaddress.ip_address(host).is_loopback


@pytest.mark.acceptance("AC-05")
def test_audit_with_a_local_model_makes_no_external_requests(
    vulnerable: Path,  # noqa: F811
    local_model: FakeLLM,
    connections: list[Any],
    sandbox_networks: list[str],
) -> None:
    """Sandboxed audit + local-model triage under local-only: only loopback traffic."""
    require_images(GITLEAKS_IMAGE, SEMGREP_IMAGE)
    cfg_path = vulnerable / ".ai-coding.yml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["security"]["network"] == "local-only"  # the secure default of `init`
    cfg["ai"] = {"enabled": True, "provider": "ollama", "model": "qwen2.5-coder",
                 "base_url": f"{local_model.url}/v1"}  # fmt: skip
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    argv = ["audit", "-C", str(vulnerable), "-b", "docker", "-t", "gitleaks", "-t", "semgrep",
            "--json"]  # fmt: skip
    plain = json.loads(runner.invoke(app, argv).output)
    local_model.scripts["security"] = [{
        "summary": "Committed key and shell injection.",
        "assessments": [{"finding_id": f["fingerprint"][:12], "verdict": "confirmed",
                         "severity": "high", "evidence": "seen in code",
                         "recommendation": "fix it"} for f in plain["findings"]],
        "additional_risks": [],
    }]  # fmt: skip

    result = runner.invoke(app, [*argv, "--triage"])
    assert result.exit_code == ExitCode.INVALID, result.output  # findings fail the gates
    report = json.loads(result.output)
    paths = {(f["tool"], f["path"]) for f in report["findings"]}
    assert ("gitleaks", "app/settings.py") in paths and ("semgrep", "app/main.py") in paths
    assert report["triage"]["status"] == "done", report["triage"]  # the local model was used
    assert local_model.requests, "the triage request reached the local model"

    inet = [a for a in connections if not isinstance(a, str | bytes)]
    assert inet and all(_loopback(a) for a in connections), connections
    assert sandbox_networks and set(sandbox_networks) == {"none"}, sandbox_networks
    assert AWS_KEY not in json.dumps(local_model.requests)  # redacted before the model
