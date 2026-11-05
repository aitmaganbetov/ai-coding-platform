"""B-603 in the real sandbox: the project's Playwright tests and k6 scripts."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from ai_coding.config.models import DockerConfig
from ai_coding.executors import DockerSandboxExecutor
from ai_coding.tools.e2e import K6_IMAGE, PLAYWRIGHT_IMAGE, K6Adapter, PlaywrightAdapter
from ai_coding.tools.project import ProjectToolRunner
from ai_coding.tools.testrun import PYTHON_IMAGE
from tests.integration.conftest import require_images
from tests.integration.test_egress import _internet

pytestmark = [pytest.mark.integration, pytest.mark.docker]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pilot-playwright"


def _runner(root: Path, *, allowed: set[str]) -> ProjectToolRunner:
    executor = DockerSandboxExecutor(docker_config=DockerConfig(network="bridge"))
    return ProjectToolRunner(executor, root, network_allowed=lambda h: h in allowed,
                             sandbox_network=True, timeout_seconds=600)  # fmt: skip


def _readable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def test_playwright_runs_the_projects_browser_tests(tmp_path: Path) -> None:
    require_images(PLAYWRIGHT_IMAGE)
    root = tmp_path / "app"
    shutil.copytree(FIXTURE, root)
    if not _internet():  # e.g. a TLS-intercepting dev sandbox: install on the host instead
        npm = shutil.which("npm")
        if npm is None:
            pytest.skip("npm is not available to install the fixture's dependencies")
        subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=root,
                       check=True, capture_output=True)  # fmt: skip
    _readable(root)
    run = asyncio.run(_runner(root, allowed={"registry.npmjs.org"}).run(PlaywrightAdapter()))
    assert run.succeeded and not run.skipped, run.error
    assert run.version == "1.56.1"
    (finding,) = run.findings  # one of the two tests fails, on purpose
    assert finding.path == "tests/ui.spec.js" and finding.line == 8
    assert finding.title == "E2E test failed: shows the total"
    assert not (FIXTURE / "test-results").exists()  # the project itself is untouched


@pytest.fixture
def system_under_test() -> Iterator[str]:
    """A small HTTP service on the Docker bridge, standing in for a staging system."""
    require_images(PYTHON_IMAGE)
    name = f"aic-sut-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "bridge",
                    PYTHON_IMAGE, "python3", "-m", "http.server", "80"],
                   check=True, capture_output=True)  # fmt: skip
    try:
        address = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
            check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip
        yield address
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def test_k6_loads_the_system_under_test_through_the_proxy(
    tmp_path: Path, system_under_test: str
) -> None:
    require_images(K6_IMAGE, PYTHON_IMAGE)
    root = tmp_path / "app"
    (root / "k6").mkdir(parents=True)
    (root / "k6" / "smoke.js").write_text(
        'import http from "k6/http";\nimport { check } from "k6";\n'
        "export const options = { iterations: 20, thresholds: {\n"
        '  http_req_failed: ["rate<0.01"], http_req_duration: ["p(95)<0.001"] } };\n'
        f'export default function () {{ check(http.get("http://{system_under_test}/"), '
        '{ "status 200": (r) => r.status === 200 }); }\n',
        encoding="utf-8",
    )
    hosts = [f"{system_under_test}:80"]
    doc = {"schema_version": "2.1", "project": {"name": "app"},
           "quality_gates": {"tests": {"performance_hosts": hosts}}}  # fmt: skip
    (root / ".ai-coding.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    _readable(root)
    run = asyncio.run(_runner(root, allowed={f"{system_under_test}:80"}).run(K6Adapter()))
    assert run.succeeded and not run.skipped, run.error
    assert run.version == "1.3.0"
    # The requests got through (no failures), and the impossible latency threshold
    # is reported as a finding with the measured values.
    (finding,) = run.findings
    assert finding.rule_id == "threshold:http_req_duration" and finding.path == "k6/smoke.js"
    assert "p(95)<0.001" in finding.title and json.dumps(finding.evidence)
    denied = asyncio.run(_runner(root, allowed=set()).run(K6Adapter()))
    assert denied.skipped and "security policy" in str(denied.error)
