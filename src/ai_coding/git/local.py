"""``git`` CLI adapter.

Design notes (ADR-0008):

* ``git`` is invoked with an argv list — never through a shell — with a timeout.
* Repository content is untrusted (ТЗ §12). A hostile ``.git/config`` can make
  ``git status`` execute code via ``core.fsmonitor``; hooks run on checkout and
  commit. Every invocation therefore disables fsmonitor and hooks, and terminal
  prompts, so the platform never executes repository-controlled programs.
* Protected branches are enforced client-side here *and* should be enforced on the
  hosting side (GitHub Rulesets, S4).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ai_coding.git.base import (
    BranchName,
    FileStatus,
    RepositoryStatus,
    Worktree,
    validate_branch_name,
)
from ai_coding.git.errors import (
    GitCommandError,
    GitNotFoundError,
    NotARepositoryError,
    ProtectedBranchError,
)

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_PROTECTED = ("main", "master", "release/*")

_SAFETY_CONFIG: tuple[str, ...] = (
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.quotepath=false",
    "-c", "color.ui=false",
)  # fmt: skip


def git_available() -> bool:
    """Whether a ``git`` executable is on ``PATH``."""
    return shutil.which("git") is not None


def _git_executable() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise GitNotFoundError("git executable not found on PATH")
    return exe


def _run(
    args: Sequence[str],
    cwd: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    try:
        proc = subprocess.run(
            [_git_executable(), *_SAFETY_CONFIG, *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(args, -1, f"timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stderr)
    return proc


class LocalGitRepository:
    """:class:`~ai_coding.git.base.GitRepository` implementation over the git CLI."""

    def __init__(
        self,
        root: Path,
        *,
        protected_branches: Sequence[str] = DEFAULT_PROTECTED,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._root = root
        self._protected = tuple(protected_branches)
        self._timeout = timeout

    # ------------------------------------------------------------ factories --
    @classmethod
    def open(cls, path: Path, **kwargs: object) -> LocalGitRepository:
        """Open the repository containing ``path``."""
        path = path.resolve()
        if not path.exists():
            raise NotARepositoryError(f"{path} does not exist")
        cwd = path if path.is_dir() else path.parent
        proc = _run(["rev-parse", "--show-toplevel"], cwd, check=False)
        if proc.returncode != 0:
            raise NotARepositoryError(f"{path} is not inside a git working tree")
        root = Path(proc.stdout.strip()).resolve()
        return cls(root, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def init(
        cls, path: Path, *, initial_branch: str = "main", **kwargs: object
    ) -> LocalGitRepository:
        """``git init`` a new repository at ``path``."""
        validate_branch_name(initial_branch)
        path.mkdir(parents=True, exist_ok=True)
        _run(["init", "--quiet", f"--initial-branch={initial_branch}"], path)
        return cls(path.resolve(), **kwargs)  # type: ignore[arg-type]

    @staticmethod
    def is_repository(path: Path) -> bool:
        """Whether ``path`` is inside a git working tree."""
        if not git_available() or not path.exists():
            return False
        cwd = path if path.is_dir() else path.parent
        proc = _run(["rev-parse", "--is-inside-work-tree"], cwd, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    # ----------------------------------------------------------------- core --
    @property
    def root(self) -> Path:
        """Top-level directory of the working tree."""
        return self._root

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _run(args, self._root, timeout=self._timeout, check=check)

    def current_branch(self) -> str | None:
        """Checked-out branch name, ``None`` if detached."""
        proc = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def head_commit(self) -> str | None:
        """Full SHA of HEAD, ``None`` if there are no commits."""
        proc = self._git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def status(self) -> RepositoryStatus:
        """Current branch, HEAD and changed files."""
        proc = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        return RepositoryStatus(
            root=self._root,
            branch=self.current_branch(),
            head=self.head_commit(),
            files=tuple(_parse_porcelain_z(proc.stdout)),
        )

    def branches(self) -> list[str]:
        """Local branch names, sorted."""
        proc = self._git("for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return sorted(line for line in proc.stdout.splitlines() if line)

    def branch_exists(self, name: str) -> bool:
        """Whether a local branch exists."""
        validate_branch_name(name)
        proc = self._git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        return proc.returncode == 0

    def remote_url(self, remote: str = "origin") -> str | None:
        """URL of a remote, ``None`` if not configured."""
        validate_branch_name(remote)
        proc = self._git("remote", "get-url", "--", remote, check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def create_branch(self, name: str, start_point: str = "HEAD") -> BranchName:
        """Create a branch at ``start_point`` without checking it out."""
        branch = validate_branch_name(name)
        self.ensure_not_protected(branch)
        self._check_start_point(start_point)
        self._git("branch", "--no-track", "--", branch, start_point)
        return branch

    # ------------------------------------------------------------ worktrees --
    def worktrees(self) -> list[Worktree]:
        """All worktrees, main first."""
        proc = self._git("worktree", "list", "--porcelain", "-z")
        return _parse_worktrees_z(proc.stdout)

    def add_worktree(self, path: Path, branch: str, *, base: str = "HEAD") -> Worktree:
        """Create ``branch`` at ``base`` and check it out in a new worktree at ``path``.

        Changes made by agents happen here, never in the user's working copy (ТЗ §18).
        """
        name = validate_branch_name(branch)
        self.ensure_not_protected(name)
        self._check_start_point(base)
        target = path.resolve()
        if target.exists() and any(target.iterdir()):
            raise GitCommandError(["worktree", "add"], 1, f"{target} exists and is not empty")
        self._git("worktree", "add", "--quiet", "-b", name, "--", str(target), base)
        for wt in self.worktrees():
            if _same_path(wt.path, target):
                return wt
        raise GitCommandError(["worktree", "list"], 1, f"created worktree {target} not listed")

    def remove_worktree(self, path: Path, *, force: bool = False) -> None:
        """Remove a linked worktree (refuses the main worktree)."""
        target = path.resolve()
        if _same_path(target, self._root):
            raise GitCommandError(["worktree", "remove"], 1, "refusing to remove the main worktree")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        self._git(*args, "--", str(target))

    # --------------------------------------------------------------- commits --
    def commit(self, message: str, *, paths: Sequence[str] | None = None) -> str:
        """Stage ``paths`` (or everything) and commit on the current, non-protected branch."""
        if not message.strip():
            raise GitCommandError(["commit"], 1, "commit message must not be empty")
        branch = self.current_branch()
        if branch is None:
            raise ProtectedBranchError("refusing to commit on a detached HEAD")
        self.ensure_not_protected(branch)
        if paths:
            self._git("add", "--", *paths)
        else:
            self._git("add", "--all")
        self._git("commit", "--quiet", "--no-verify", "-m", message)
        sha = self.head_commit()
        if sha is None:  # pragma: no cover - commit succeeded, HEAD must exist
            raise GitCommandError(["commit"], 1, "HEAD missing after commit")
        return sha

    # ------------------------------------------------------------ protection --
    @property
    def protected_branches(self) -> tuple[str, ...]:
        """Configured protected-branch patterns."""
        return self._protected

    def is_protected(self, branch: str) -> bool:
        """Whether ``branch`` matches a protected pattern (fnmatch, ``*`` spans '/')."""
        return any(fnmatch.fnmatchcase(branch, pattern) for pattern in self._protected)

    def ensure_not_protected(self, branch: str) -> None:
        """Raise :class:`ProtectedBranchError` if ``branch`` is protected."""
        if self.is_protected(branch):
            raise ProtectedBranchError(
                f"branch '{branch}' is protected; changes must go through a separate branch and PR"
            )

    def _check_start_point(self, ref: str) -> None:
        if not ref or ref.startswith("-"):
            raise GitCommandError(["rev-parse"], 1, f"invalid start point: {ref!r}")
        proc = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if proc.returncode != 0:
            raise GitCommandError(["rev-parse"], proc.returncode, f"unknown revision: {ref!r}")


# ---------------------------------------------------------------- parsers ---
def _parse_porcelain_z(output: str) -> list[FileStatus]:
    entries = output.split("\0")
    result: list[FileStatus] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        original = None
        if x in {"R", "C"} or y in {"R", "C"}:
            original = entries[i] if i < len(entries) else None
            i += 1
        result.append(FileStatus(index=x, worktree=y, path=path, original_path=original))
    return result


def _parse_worktrees_z(output: str) -> list[Worktree]:
    worktrees: list[Worktree] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if "worktree" in current:
            branch = current.get("branch")
            worktrees.append(
                Worktree(
                    path=Path(current["worktree"]),
                    head=current.get("HEAD"),
                    branch=branch.removeprefix("refs/heads/") if branch else None,
                    is_main=not worktrees,
                    locked="locked" in current,
                    prunable="prunable" in current,
                )
            )
        current.clear()

    for field_ in output.split("\0"):
        if not field_:
            flush()
            continue
        key, _, value = field_.partition(" ")
        current[key] = value
    flush()
    return worktrees


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or a.samefile(b)
    except OSError:
        return False
