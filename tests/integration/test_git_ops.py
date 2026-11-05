"""Git: safe clone (FR-01), diff/patch and rollback (FR-05, ТЗ §18)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.git import (
    GitCommandError,
    InvalidRefError,
    LocalGitRepository,
    ProtectedBranchError,
    validate_remote_url,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
    ],
)
def test_allowed_urls(url: str) -> None:
    validate_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc", "/srv/repo", "../repo", "ext::sh -c touch% /tmp/pwned", "-uhttps://x",
        "http://insecure.example.com/r.git", "https://host/a b", "https://host::evil", "",
        "fd::3", "git://host/repo",
    ],
)  # fmt: skip
def test_refused_urls(url: str) -> None:
    with pytest.raises(InvalidRefError):
        validate_remote_url(url)


def _bare_origin(git_repo: LocalGitRepository, tmp_path: Path) -> str:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(git_repo.root), str(origin)], check=True)
    return origin.as_uri()


def test_clone_shallow_and_guards(git_repo: LocalGitRepository, tmp_path: Path) -> None:
    url = _bare_origin(git_repo, tmp_path)
    with pytest.raises(InvalidRefError):
        LocalGitRepository.clone(url, tmp_path / "refused")  # file:// is refused by default
    clone = LocalGitRepository.clone(url, tmp_path / "work" / "clone", allow_file_protocol=True)
    assert (clone.root / "README.md").is_file()
    assert clone.current_branch() == "main"
    assert clone.remote_url() == url
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=clone.root,
                           capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    assert count == "1"
    with pytest.raises(GitCommandError, match="not empty"):
        LocalGitRepository.clone(url, clone.root, allow_file_protocol=True)
    with pytest.raises(GitCommandError, match="depth"):
        LocalGitRepository.clone(url, tmp_path / "d", depth=0, allow_file_protocol=True)
    with pytest.raises(InvalidRefError):
        LocalGitRepository.clone(url, tmp_path / "b", branch="-x", allow_file_protocol=True)
    full = LocalGitRepository.clone(url, tmp_path / "full", depth=None, branch="main",
                                    allow_file_protocol=True)  # fmt: skip
    assert full.head_commit() == git_repo.head_commit()


def test_clone_failure_is_reported(tmp_path: Path, git_env: None) -> None:
    with pytest.raises(GitCommandError):
        LocalGitRepository.clone((tmp_path / "missing.git").as_uri(), tmp_path / "x",
                                 allow_file_protocol=True)  # fmt: skip


@pytest.fixture
def feature(git_repo: LocalGitRepository) -> LocalGitRepository:
    subprocess.run(["git", "checkout", "-q", "-b", "ai/feature"], cwd=git_repo.root, check=True)
    return git_repo


def test_diff_and_stat(feature: LocalGitRepository) -> None:
    base = feature.head_commit()
    assert base is not None
    (feature.root / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (feature.root / "logo.bin").write_bytes(b"\x00\x01\x02")
    feature.commit("feat: change")
    diff = feature.diff(base)
    assert "+world" in diff and "diff --git a/README.md" in diff
    assert sorted(feature.diff_stat(base)) == [("README.md", 1, 0), ("logo.bin", -1, -1)]
    assert feature.diff(base, paths=["logo.bin"]).count("diff --git") == 1
    with pytest.raises(GitCommandError, match="invalid start point"):
        feature.diff("--output=/tmp/x")


def test_apply_patch_check_and_apply(feature: LocalGitRepository) -> None:
    patch = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1,2 @@\n hello\n+patched\n"
    )
    feature.apply_patch(patch, check_only=True)
    assert (feature.root / "README.md").read_text(encoding="utf-8") == "hello\n"
    feature.apply_patch(patch)
    assert (feature.root / "README.md").read_text(encoding="utf-8") == "hello\npatched\n"
    with pytest.raises(GitCommandError, match="apply"):
        feature.apply_patch(patch)  # no longer applies


def test_patch_refused_on_protected_or_detached(git_repo: LocalGitRepository) -> None:
    with pytest.raises(ProtectedBranchError):
        git_repo.apply_patch("")
    head = git_repo.head_commit()
    assert head is not None
    subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=git_repo.root, check=True)
    with pytest.raises(ProtectedBranchError, match="detached"):
        git_repo.apply_patch("")
    with pytest.raises(ProtectedBranchError, match="detached"):
        git_repo.rollback_to(head)


def test_rollback(feature: LocalGitRepository) -> None:
    base = feature.head_commit()
    assert base is not None
    (feature.root / "x.txt").write_text("x", encoding="utf-8")
    feature.commit("feat: x")
    (feature.root / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(GitCommandError, match="uncommitted"):
        feature.rollback_to(base)
    assert feature.rollback_to(base, discard_changes=True) == base
    assert not (feature.root / "x.txt").exists()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=feature.root, check=True)
    with pytest.raises(ProtectedBranchError):
        feature.rollback_to(base)
