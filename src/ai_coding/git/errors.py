"""Git adapter errors."""

from __future__ import annotations

from collections.abc import Sequence


class GitError(Exception):
    """Base class for Git adapter errors."""


class GitNotFoundError(GitError):
    """The ``git`` executable is not installed or not on ``PATH``."""


class NotARepositoryError(GitError):
    """The path is not inside a Git working tree."""


class InvalidRefError(GitError):
    """A branch/ref name failed validation (``git check-ref-format`` rules + platform policy)."""


class ProtectedBranchError(GitError):
    """An operation would modify a protected branch directly (AC-03)."""


class GitCommandError(GitError):
    """``git`` exited with a non-zero status."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(self.args_list)} failed ({returncode}): {self.stderr}")
