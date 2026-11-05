"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_coding.git import LocalGitRepository
from ai_coding.persistence import Database, upgrade_to_head
from tests.helpers import OPEN_DATABASES

FIXTURES = Path(__file__).parent / "fixtures"
pytest_plugins = ["tests.acceptance_plugin"]


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory with static test fixtures."""
    return FIXTURES


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Hermetic git identity/config so tests never depend on the developer's ~/.gitconfig."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    yield


@pytest.fixture
def git_repo(tmp_path: Path, git_env: None) -> LocalGitRepository:
    """A fresh repository on ``main`` with one commit."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = LocalGitRepository.init(tmp_path / "repo", initial_branch="main")
    (repo.root / "README.md").write_text("hello\n", encoding="utf-8")
    # Commit the initial state with plain git: main is protected for the adapter.
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=repo.root,
        check=True,
    )
    return repo


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """Migrated SQLite database in a temporary directory."""
    database = Database(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    upgrade_to_head(database)
    yield database
    database.dispose()


@pytest.fixture(autouse=True)
def _dispose_databases() -> Iterator[None]:
    """Close every database opened through tests.helpers.make_orchestrator."""
    yield
    while OPEN_DATABASES:
        OPEN_DATABASES.pop().dispose()


@pytest.fixture(autouse=True)
def _no_database_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's AI_CODING_DATABASE_URL leak into tests."""
    monkeypatch.delenv("AI_CODING_DATABASE_URL", raising=False)


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """A running fake GitHub API; the environment points the platform at it."""
    from tests.fake_github import FakeGitHub  # noqa: PLC0415

    fake = FakeGitHub().start()
    monkeypatch.setenv("GITHUB_API_URL", fake.url)
    monkeypatch.setenv("GITHUB_TOKEN", fake.token)
    monkeypatch.setenv("GITHUB_REPOSITORY", fake.repo)
    for var in ("AI_CODING_GITHUB_TOKEN", "AI_CODING_GITHUB_APP_ID"):
        monkeypatch.delenv(var, raising=False)
    yield fake
    fake.stop()
