"""Integration tests for the git CLI adapter (real ``git`` binary, temp repositories)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.git import (
    GitCommandError,
    GitRepository,
    InvalidRefError,
    LocalGitRepository,
    NotARepositoryError,
    ProtectedBranchError,
)
from ai_coding.git.base import validate_branch_name
from ai_coding.git.local import _parse_porcelain_z, _parse_worktrees_z

pytestmark = pytest.mark.integration


def test_implements_port(git_repo: LocalGitRepository) -> None:
    assert isinstance(git_repo, GitRepository)


def test_open_from_subdirectory(git_repo: LocalGitRepository) -> None:
    sub = git_repo.root / "pkg"
    sub.mkdir()
    assert LocalGitRepository.open(sub).root == git_repo.root
    assert LocalGitRepository.open(git_repo.root / "README.md").root == git_repo.root


def test_open_rejects_non_repo(tmp_path: Path, git_env: None) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotARepositoryError):
        LocalGitRepository.open(plain)
    with pytest.raises(NotARepositoryError):
        LocalGitRepository.open(tmp_path / "missing")
    assert not LocalGitRepository.is_repository(plain)
    assert not LocalGitRepository.is_repository(tmp_path / "missing")


def test_status_clean_and_dirty(git_repo: LocalGitRepository) -> None:
    status = git_repo.status()
    assert status.is_clean
    assert status.branch == "main"
    assert status.head is not None and len(status.head) == 40
    assert not status.is_detached

    (git_repo.root / "README.md").write_text("changed\n", encoding="utf-8")
    (git_repo.root / "new file.txt").write_text("x", encoding="utf-8")
    status = git_repo.status()
    assert not status.is_clean
    by_path = {f.path: f for f in status.files}
    assert by_path["README.md"].worktree == "M"
    assert by_path["new file.txt"].is_untracked


def test_status_reports_renames(git_repo: LocalGitRepository) -> None:
    subprocess.run(["git", "mv", "README.md", "DOCS.md"], cwd=git_repo.root, check=True)
    files = git_repo.status().files
    assert files[0].index == "R"
    assert files[0].path == "DOCS.md"
    assert files[0].original_path == "README.md"


def test_empty_repository(tmp_path: Path, git_env: None) -> None:
    repo = LocalGitRepository.init(tmp_path / "empty")
    assert repo.head_commit() is None
    assert repo.current_branch() == "main"
    assert repo.branches() == []


def test_detached_head(git_repo: LocalGitRepository) -> None:
    head = git_repo.head_commit()
    assert head is not None
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=git_repo.root, check=True)
    assert git_repo.current_branch() is None
    assert git_repo.status().is_detached
    with pytest.raises(ProtectedBranchError, match="detached"):
        git_repo.commit("x")


def test_branches_and_create(git_repo: LocalGitRepository) -> None:
    assert git_repo.branches() == ["main"]
    git_repo.create_branch("feature/login")
    assert git_repo.branch_exists("feature/login")
    assert git_repo.branches() == ["feature/login", "main"]
    assert git_repo.current_branch() == "main"  # not checked out


def test_create_branch_guards(git_repo: LocalGitRepository) -> None:
    with pytest.raises(ProtectedBranchError):
        git_repo.create_branch("release/1.0")
    with pytest.raises(InvalidRefError):
        git_repo.create_branch("--upload-pack=evil")
    with pytest.raises(GitCommandError, match="invalid start point"):
        git_repo.create_branch("ok", start_point="--evil")
    with pytest.raises(GitCommandError, match="unknown revision"):
        git_repo.create_branch("ok", start_point="does-not-exist")


def test_remote_url(git_repo: LocalGitRepository) -> None:
    assert git_repo.remote_url() is None
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/r.git"],
        cwd=git_repo.root,
        check=True,
    )
    assert git_repo.remote_url() == "https://example.invalid/r.git"


def test_worktree_lifecycle_isolates_changes(git_repo: LocalGitRepository, tmp_path: Path) -> None:
    wt_path = tmp_path / "wt" / "task-1"
    wt = git_repo.add_worktree(wt_path, "ai/task-1")
    assert wt.branch == "ai/task-1"
    assert not wt.is_main
    assert (wt_path / "README.md").is_file()

    listed = git_repo.worktrees()
    assert listed[0].is_main and listed[0].branch == "main"
    assert len(listed) == 2

    (wt_path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    worktree_repo = LocalGitRepository.open(wt_path)
    sha = worktree_repo.commit("feat: add feature")
    assert worktree_repo.head_commit() == sha
    # The user's working copy and main are untouched (AC-03).
    assert not (git_repo.root / "feature.py").exists()
    assert git_repo.status().is_clean
    assert git_repo.current_branch() == "main"

    git_repo.remove_worktree(wt_path)
    assert len(git_repo.worktrees()) == 1
    assert git_repo.branch_exists("ai/task-1")  # branch survives for the PR


def test_worktree_guards(git_repo: LocalGitRepository, tmp_path: Path) -> None:
    with pytest.raises(ProtectedBranchError):
        git_repo.add_worktree(tmp_path / "w1", "main")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "f").write_text("x", encoding="utf-8")
    with pytest.raises(GitCommandError, match="not empty"):
        git_repo.add_worktree(occupied, "ai/x")
    with pytest.raises(GitCommandError, match="main worktree"):
        git_repo.remove_worktree(git_repo.root)


def test_dirty_worktree_removal_requires_force(
    git_repo: LocalGitRepository, tmp_path: Path
) -> None:
    path = tmp_path / "dirty"
    git_repo.add_worktree(path, "ai/dirty")
    (path / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitCommandError):
        git_repo.remove_worktree(path)
    git_repo.remove_worktree(path, force=True)
    assert len(git_repo.worktrees()) == 1


def test_commit_on_protected_branch_refused(git_repo: LocalGitRepository) -> None:
    (git_repo.root / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ProtectedBranchError, match="protected"):
        git_repo.commit("direct push to main")


def test_commit_specific_paths_and_empty_message(git_repo: LocalGitRepository) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=git_repo.root, check=True)
    (git_repo.root / "a.txt").write_text("a", encoding="utf-8")
    (git_repo.root / "b.txt").write_text("b", encoding="utf-8")
    with pytest.raises(GitCommandError, match="empty"):
        git_repo.commit("  ")
    git_repo.commit("add a", paths=["a.txt"])
    remaining = {f.path for f in git_repo.status().files}
    assert remaining == {"b.txt"}


def test_untrusted_repository_hooks_and_fsmonitor_never_run(
    git_repo: LocalGitRepository, tmp_path: Path
) -> None:
    """ТЗ §12: repository content is untrusted; git must not execute repo-controlled programs."""
    marker = tmp_path / "pwned"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    script.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(script)], cwd=git_repo.root, check=True)
    hooks = git_repo.root / ".git" / "hooks"
    for hook in ("post-checkout", "pre-commit", "commit-msg", "post-commit"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        (hooks / hook).chmod(0o755)

    git_repo.status()
    wt = tmp_path / "wt-evil"
    git_repo.add_worktree(wt, "ai/evil")
    (wt / "f.txt").write_text("f", encoding="utf-8")
    LocalGitRepository.open(wt).commit("c")
    assert not marker.exists()


@pytest.mark.parametrize(
    "name",
    ["", "-x", "a..b", "a b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "x.lock",
     "x/", "/x", "a//b", "a@{b", "@", ".hidden", "a/.b", "end."],
)  # fmt: skip
def test_invalid_branch_names(name: str) -> None:
    with pytest.raises(InvalidRefError):
        validate_branch_name(name)


@pytest.mark.parametrize("name", ["main", "feature/x-1", "ai/run_42/task.3", "v2.1"])
def test_valid_branch_names(name: str) -> None:
    assert validate_branch_name(name) == name


def test_is_protected_patterns(tmp_path: Path) -> None:
    repo = LocalGitRepository(tmp_path, protected_branches=["main", "release/*"])
    assert repo.is_protected("main")
    assert repo.is_protected("release/2.1")
    assert not repo.is_protected("feature/main")
    assert repo.protected_branches == ("main", "release/*")


def test_parsers_handle_edge_cases() -> None:
    assert _parse_porcelain_z("") == []
    assert _parse_porcelain_z("?? a\0x\0") == [
        _parse_porcelain_z("?? a\0")[0],
    ]
    wts = _parse_worktrees_z("worktree /a\0HEAD abc\0detached\0locked\0\0")
    assert wts[0].branch is None and wts[0].locked and wts[0].is_main


def test_commit_trailers_and_lookup(git_repo: LocalGitRepository) -> None:
    assert git_repo.find_commits_by_trailer("AI-Coding-Task", "x") == []
    subprocess.run(["git", "checkout", "-q", "-b", "ai/t"], cwd=git_repo.root, check=True)
    (git_repo.root / "a.txt").write_text("a", encoding="utf-8")
    sha = git_repo.commit("feat: a", trailers={"AI-Coding-Task": "run1/a", "AI-Coding-Run": "run1"})
    (git_repo.root / "b.txt").write_text("b", encoding="utf-8")
    git_repo.commit("feat: b", trailers={"AI-Coding-Task": "run1/b"})
    assert git_repo.find_commits_by_trailer("AI-Coding-Task", "run1/a") == [sha]
    assert git_repo.find_commits_by_trailer("AI-Coding-Run", "run1") == [sha]
    assert git_repo.find_commits_by_trailer("AI-Coding-Task", "run1/zzz") == []
    assert git_repo.find_commits_by_trailer("AI-Coding-Task", "run1/a", ref="ai/t") == [sha]
    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", sha], cwd=git_repo.root,
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert message.strip().endswith("AI-Coding-Task: run1/a\nAI-Coding-Run: run1")


@pytest.mark.parametrize(
    "trailers", [{"bad key": "v"}, {"K": "multi\nline"}, {"K": " "}, {"-x": "v"}]
)
def test_invalid_trailers_rejected(git_repo: LocalGitRepository, trailers: dict[str, str]) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", "ai/t"], cwd=git_repo.root, check=True)
    (git_repo.root / "a.txt").write_text("a", encoding="utf-8")
    with pytest.raises(GitCommandError, match="invalid trailer"):
        git_repo.commit("x", trailers=trailers)
    with pytest.raises(GitCommandError, match="invalid trailer key"):
        git_repo.find_commits_by_trailer("bad key", "v")
    with pytest.raises(GitCommandError, match="invalid start point"):
        git_repo.find_commits_by_trailer("K", "v", ref="--upload-pack=x")
