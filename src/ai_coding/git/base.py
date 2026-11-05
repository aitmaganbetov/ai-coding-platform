"""Port (interface) and value objects for Git operations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NewType, Protocol, runtime_checkable

from ai_coding.git.errors import InvalidRefError

BranchName = NewType("BranchName", str)

# Conservative subset of git-check-ref-format(1) plus "no leading '-'" (option injection).
_FORBIDDEN_REF = re.compile(
    r"(^-)|(^/)|(/$)|(\.\.)|(@\{)|(//)|([\x00-\x20\x7f~^:?*\[\\])"
    r"|(\.lock$)|(\.$)|(/\.)|(^\.)"
)


def validate_branch_name(name: str) -> BranchName:
    """Validate a branch name or raise :class:`InvalidRefError`."""
    if not name or len(name) > 255 or name == "@" or _FORBIDDEN_REF.search(name):
        raise InvalidRefError(f"invalid branch name: {name!r}")
    return BranchName(name)


@dataclass(frozen=True, slots=True)
class FileStatus:
    """One entry of ``git status --porcelain=v1``."""

    index: str  # staged status letter (X)
    worktree: str  # unstaged status letter (Y)
    path: str
    original_path: str | None = None

    @property
    def is_untracked(self) -> bool:
        """``??`` entries."""
        return self.index == "?" and self.worktree == "?"


@dataclass(frozen=True, slots=True)
class RepositoryStatus:
    """Snapshot of repository state used by FR-01 (access and Git state checks)."""

    root: Path
    branch: str | None  # ``None`` when HEAD is detached
    head: str | None  # ``None`` in a repository without commits
    files: tuple[FileStatus, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        """No staged, unstaged or untracked changes."""
        return not self.files

    @property
    def is_detached(self) -> bool:
        """HEAD is detached (or unborn branch with no name)."""
        return self.branch is None


@dataclass(frozen=True, slots=True)
class Worktree:
    """A linked or main worktree."""

    path: Path
    head: str | None
    branch: str | None
    is_main: bool = False
    locked: bool = False
    prunable: bool = False


@runtime_checkable
class GitRepository(Protocol):
    """Operations the platform needs from a Git working copy."""

    @property
    def root(self) -> Path:
        """Top-level directory of the working tree."""
        ...

    def status(self) -> RepositoryStatus:
        """Current branch, HEAD and changed files."""
        ...

    def current_branch(self) -> str | None:
        """Checked-out branch name, ``None`` if detached."""
        ...

    def head_commit(self) -> str | None:
        """Full SHA of HEAD, ``None`` if there are no commits."""
        ...

    def branches(self) -> list[str]:
        """Local branch names."""
        ...

    def branch_exists(self, name: str) -> bool:
        """Whether a local branch exists."""
        ...

    def remote_url(self, remote: str = "origin") -> str | None:
        """URL of a remote, ``None`` if not configured."""
        ...

    def create_branch(self, name: str, start_point: str = "HEAD") -> BranchName:
        """Create a branch without checking it out."""
        ...

    def worktrees(self) -> list[Worktree]:
        """All worktrees, main first."""
        ...

    def add_worktree(self, path: Path, branch: str, *, base: str = "HEAD") -> Worktree:
        """Create a new branch at ``base`` checked out in a new worktree at ``path``."""
        ...

    def remove_worktree(self, path: Path, *, force: bool = False) -> None:
        """Remove a linked worktree."""
        ...

    def find_commits_by_trailer(self, key: str, value: str, *, ref: str = "--all") -> list[str]:
        """Commits carrying a given trailer (idempotent recovery)."""
        ...

    def is_protected(self, branch: str) -> bool:
        """Whether ``branch`` matches a protected-branch pattern."""
        ...

    def ensure_not_protected(self, branch: str) -> None:
        """Raise :class:`ProtectedBranchError` if ``branch`` is protected."""
        ...
