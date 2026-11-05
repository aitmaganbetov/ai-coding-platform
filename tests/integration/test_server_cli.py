"""``ai-coding server`` CLI and a real server process with an embedded worker (S8)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode

pytestmark = pytest.mark.integration
runner = CliRunner()
PASSWORD = "correct horse battery staple"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    roots = tmp_path / "repos"
    (roots / "alpha").mkdir(parents=True)
    (roots / "alpha" / "main.py").write_text("print(1)\n", encoding="utf-8")
    values = {
        "AI_CODING_SERVER_DATA_DIR": str(tmp_path / "data"),
        "AI_CODING_SERVER_DATABASE_URL": f"sqlite:///{tmp_path / 'server.db'}",
        "AI_CODING_SERVER_ALLOWED_ROOTS": str(roots),
        "AI_CODING_SERVER_METRICS_TOKEN": "metrics-secret-xyz",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_bootstrap_users_and_tokens(env: dict[str, str]) -> None:
    assert runner.invoke(app, ["server", "migrate"]).exit_code == 0
    added = runner.invoke(
        app, ["server", "user", "add", "root", "--admin", "--password-stdin"], input=PASSWORD + "\n"
    )
    assert added.exit_code == 0, added.output
    weak = runner.invoke(app, ["server", "user", "add", "bob", "--password-stdin"], input="x\n")
    assert weak.exit_code == ExitCode.INVALID and "at least 12" in weak.output
    dup = runner.invoke(
        app, ["server", "user", "add", "root", "--password-stdin"], input=PASSWORD + "\n"
    )
    assert dup.exit_code == ExitCode.INVALID and "already exists" in dup.output
    token = runner.invoke(app, ["server", "token", "create", "root", "--name", "ci"])
    assert token.exit_code == 0
    assert token.stdout.splitlines()[0].startswith("aic_")  # only the token on stdout
    missing = runner.invoke(app, ["server", "token", "create", "ghost"])
    assert missing.exit_code == ExitCode.NOT_FOUND
    changed = runner.invoke(
        app,
        ["server", "user", "password", "root", "--password-stdin"],
        input="another long password\n",
    )
    assert changed.exit_code == 0
    shown = json.loads(runner.invoke(app, ["server", "settings"]).output)
    assert shown["metrics_token"] == "set" and "metrics-secret" not in json.dumps(shown)
    assert shown["workers"] == 10


def test_invalid_settings_are_reported(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_CODING_SERVER_WORKERS", "0")
    result = runner.invoke(app, ["server", "migrate"])
    assert result.exit_code == ExitCode.INVALID and "AI_CODING_SERVER_WORKERS" in result.output


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_server(env: dict[str, str]) -> Iterator[str]:
    runner.invoke(app, ["server", "migrate"])
    runner.invoke(
        app, ["server", "user", "add", "root", "--admin", "--password-stdin"], input=PASSWORD + "\n"
    )
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from ai_coding.cli.app import main; main()",
            "server",
            "serve",
            "--port",
            str(port),
            "--with-worker",
        ],
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail(f"server did not start: {proc.communicate()[0][-2000:]}")
        yield url
    finally:
        proc.terminate()
        proc.communicate(timeout=30)  # reap the process and close its pipe


def test_real_server_process_runs_a_discovery(live_server: str, env: dict[str, str]) -> None:
    url = live_server
    login = httpx.post(f"{url}/api/v1/auth/login", json={"username": "root", "password": PASSWORD})
    assert login.status_code == 201
    h = {"Authorization": f"Bearer {login.json()['token']}"}
    root = str(Path(env["AI_CODING_SERVER_ALLOWED_ROOTS"]) / "alpha")
    project = httpx.post(f"{url}/api/v1/projects", headers=h, json={"name": "alpha", "path": root})
    assert project.status_code == 201, project.text
    run = httpx.post(
        f"{url}/api/v1/projects/{project.json()['id']}/runs", headers=h, json={"kind": "discover"}
    )
    rid = run.json()["id"]
    deadline = time.monotonic() + 60
    state = run.json()["state"]
    while state not in {"COMPLETED", "FAILED"} and time.monotonic() < deadline:
        time.sleep(0.3)
        state = httpx.get(f"{url}/api/v1/runs/{rid}", headers=h).json()["state"]
    assert state == "COMPLETED"
    assert httpx.get(f"{url}/api/v1/runs/{rid}/artifacts", headers=h).json()
    metrics = httpx.get(
        f"{url}/metrics", headers={"Authorization": "Bearer metrics-secret-xyz"}
    ).text
    assert 'aicoding_runs{state="COMPLETED"} 1' in metrics
    served = httpx.get(f"{url}/healthz")
    assert "server" not in {k.lower() for k in served.headers}  # no server banner
