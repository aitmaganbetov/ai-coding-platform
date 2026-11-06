"""``ai-coding server`` commands in process: token scoping and revocation, export,
purge, password prompts and errors, and how ``serve`` / ``worker`` are wired (uvicorn
and the worker loop replaced by recorders, so no port or process is needed).

The real server process is exercised by ``test_server_cli.py``; this file covers the
command code paths that a subprocess cannot measure.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import uuid
import zipfile
from pathlib import Path
from typing import Any

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
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _cli(*args: str, stdin: str | None = None) -> Any:
    return runner.invoke(app, ["server", *args], input=stdin)


@pytest.fixture
def seeded(env: dict[str, str], tmp_path: Path) -> uuid.UUID:
    """Users ``root`` (admin, owner of ``alpha``) and ``dev`` (developer); alpha's id."""
    from ai_coding.api.services import Principal, Server  # noqa: PLC0415
    from ai_coding.api.settings import ServerSettings  # noqa: PLC0415
    from ai_coding.domain.enums import Role  # noqa: PLC0415

    assert _cli("migrate").exit_code == 0
    assert _cli("user", "add", "root", "--admin", "--password-stdin",
                stdin=PASSWORD + "\n").exit_code == 0  # fmt: skip
    assert _cli("user", "add", "dev", "--password-stdin", stdin=PASSWORD + "\n").exit_code == 0
    server = Server(ServerSettings.from_env())
    try:
        root = server.login("root", PASSWORD)
        assert root is not None
        project = server.create_project(root, "alpha", path=str(tmp_path / "repos" / "alpha"))
        server.set_member(root, project, "dev", Role.DEVELOPER)
    finally:
        server.close()
    assert isinstance(root, Principal)
    return project


def _authenticates(token: str) -> bool:
    from ai_coding.api.services import Server  # noqa: PLC0415
    from ai_coding.api.settings import ServerSettings  # noqa: PLC0415

    server = Server(ServerSettings.from_env())
    try:
        return server.authenticate(token) is not None
    finally:
        server.close()


def test_project_scoped_tokens_and_revocation(seeded: uuid.UUID) -> None:
    scoped = _cli("token", "create", "dev", "--project", "alpha", "--role", "developer")
    assert scoped.exit_code == 0, scoped.output
    token = scoped.stdout.splitlines()[0]
    token_id = scoped.output.split("token ")[1].split()[0]
    assert token.startswith("aic_") and _authenticates(token)

    unknown_project = _cli("token", "create", "dev", "--project", "beta")
    assert unknown_project.exit_code == ExitCode.NOT_FOUND
    assert "project 'beta' not found" in unknown_project.output
    bad_role = _cli("token", "create", "dev", "--role", "emperor")
    assert bad_role.exit_code == ExitCode.INVALID
    unscoped_agent = _cli("token", "create", "dev", "--role", "agent")
    assert unscoped_agent.exit_code == ExitCode.INVALID and "one project" in unscoped_agent.output

    assert _cli("token", "revoke", token_id).exit_code == 0
    assert not _authenticates(token)  # takes effect at once
    assert _cli("token", "revoke", "not-a-uuid").exit_code == ExitCode.INVALID
    assert _cli("token", "revoke", str(uuid.uuid4())).exit_code == ExitCode.NOT_FOUND


def test_password_prompt_and_errors(seeded: uuid.UUID) -> None:
    prompted = _cli("user", "add", "carol", stdin=f"{PASSWORD}\n{PASSWORD}\n")
    assert prompted.exit_code == 0, prompted.output
    assert PASSWORD not in prompted.output  # hidden input is not echoed
    unknown = _cli("user", "password", "ghost", "--password-stdin", stdin=PASSWORD + "\n")
    assert unknown.exit_code == ExitCode.NOT_FOUND
    weak = _cli("user", "password", "carol", "--password-stdin", stdin="short\n")
    assert weak.exit_code == ExitCode.INVALID and "at least 12" in weak.output


def test_export_respects_rbac_and_purge_reports_counts(seeded: uuid.UUID, tmp_path: Path) -> None:
    archive = tmp_path / "alpha.zip"
    exported = _cli("export", "alpha", "--user", "root", "-o", str(archive))
    assert exported.exit_code == 0, exported.output
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as z:
        assert z.namelist()
    developer = _cli("export", "alpha", "--user", "dev", "-o", str(tmp_path / "dev.zip"))
    assert developer.exit_code == ExitCode.INVALID and "may not" in developer.output
    assert not (tmp_path / "dev.zip").exists()
    missing = _cli("export", "beta", "--user", "root", "-o", str(tmp_path / "x.zip"))
    assert missing.exit_code == ExitCode.NOT_FOUND

    purged = _cli("purge")
    assert purged.exit_code == 0
    assert set(json.loads(purged.stdout)) == {
        "runs", "artifact_files", "idempotency_records", "tokens",
    }  # fmt: skip


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    import uvicorn  # noqa: PLC0415

    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: calls.append((target, kw)))
    return calls


def test_serve_runs_one_app_or_several_processes(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uvicorn_calls: list[tuple[Any, dict[str, Any]]],
) -> None:
    from fastapi import FastAPI  # noqa: PLC0415

    from ai_coding.api.app import UI_DIR_ENV  # noqa: PLC0415

    # serve --workers exports the UI directory for its processes: restore it afterwards
    monkeypatch.setenv(UI_DIR_ENV, "")
    monkeypatch.delenv(UI_DIR_ENV)
    monkeypatch.setenv("AI_CODING_SERVER_TRUSTED_PROXIES", "10.0.0.2")
    conflicting = _cli("serve", "--workers", "2", "--with-worker")
    assert conflicting.exit_code == ExitCode.USAGE and not uvicorn_calls

    assert _cli("serve", "--port", "8123").exit_code == 0
    target, options = uvicorn_calls.pop()
    assert isinstance(target, FastAPI)
    assert options["port"] == 8123 and options["host"] == "127.0.0.1"
    assert options["proxy_headers"] is True and options["forwarded_allow_ips"] == "10.0.0.2"
    assert options["server_header"] is False

    ui = tmp_path / "ui"
    ui.mkdir()
    assert _cli("serve", "--workers", "3", "--ui", str(ui)).exit_code == 0
    target, options = uvicorn_calls.pop()
    assert target == "ai_coding.api.app:app_from_env"
    assert options["factory"] is True and options["workers"] == 3
    assert os.environ[UI_DIR_ENV] == str(ui.resolve())  # each process finds the UI


def test_worker_command_runs_the_pool_until_stopped(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_coding.api.worker import Worker  # noqa: PLC0415

    ran: list[int] = []

    async def run(self: Worker, stop: asyncio.Event) -> None:
        ran.append(self.concurrency)

    monkeypatch.setattr(Worker, "run", run)
    result = _cli("worker", "--concurrency", "3")
    assert result.exit_code == 0, result.output
    assert ran == [3] and "up to 3 concurrent runs" in result.output
