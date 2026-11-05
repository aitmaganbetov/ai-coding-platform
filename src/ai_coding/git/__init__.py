"""Git abstraction (ТЗ §4 Git adapters; ADR-0008).

``GitRepository`` is the port used by the orchestrator and executors.
``LocalGitRepository`` is the adapter backed by the ``git`` CLI. A GitHub API
adapter (S4) implements the remote-hosting port separately.
"""

from ai_coding.git.base import (
    BranchName,
    FileStatus,
    GitRepository,
    RepositoryStatus,
    Worktree,
)
from ai_coding.git.errors import (
    GitCommandError,
    GitError,
    GitNotFoundError,
    InvalidRefError,
    NotARepositoryError,
    ProtectedBranchError,
)
from ai_coding.git.local import LocalGitRepository, git_available

__all__ = [
    "BranchName",
    "FileStatus",
    "GitCommandError",
    "GitError",
    "GitNotFoundError",
    "GitRepository",
    "InvalidRefError",
    "LocalGitRepository",
    "NotARepositoryError",
    "ProtectedBranchError",
    "RepositoryStatus",
    "Worktree",
    "git_available",
]
