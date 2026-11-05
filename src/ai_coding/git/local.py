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

import base64
import fnmatch
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
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
    InvalidRefError,
    NotARepositoryError,
    ProtectedBranchError,
)
from ai_coding.security import default_redactor

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_PROTECTED = ("main", "master", "release/*")
_TRAILER_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")

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
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    env.update(extra_env or {})
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

    @classmethod
    def clone(
        cls,
        url: str,
        dest: Path,
        *,
        branch: str | None = None,
        depth: int | None = 1,
        timeout: float = 600.0,
        allow_file_protocol: bool = False,
        **kwargs: object,
    ) -> LocalGitRepository:
        """Clone a remote repository into an empty/new directory (FR-01).

        Only ``https://``, ``ssh://`` and scp-like ``user@host:path`` URLs are accepted;
        ``file://``, local paths and ``ext::`` transports are refused. Submodules are not
        fetched, hooks never run, and no credentials are prompted for.
        ``allow_file_protocol`` exists for offline tests and mirrors; the CLI never sets it.
        """
        if allow_file_protocol and url.startswith("file://"):
            protocols = ["-c", "protocol.file.allow=always"]
        else:
            validate_remote_url(url)
            protocols = []
        if branch is not None:
            validate_branch_name(branch)
        if depth is not None and depth < 1:
            raise GitCommandError(["clone"], 1, "depth must be >= 1")
        target = dest.resolve()
        if target.exists() and any(target.iterdir()):
            raise GitCommandError(["clone"], 1, f"{target} exists and is not empty")
        target.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "protocol.ssh.allow=always", *protocols,
            "clone", "--quiet", "--no-recurse-submodules",
        ]  # fmt: skip
        if depth is not None:
            args += ["--depth", str(depth)]
        if branch is not None:
            args += ["--branch", branch, "--single-branch"]
        args += ["--", url, str(target)]
        _run(args, target.parent, timeout=timeout)
        return cls(target, **kwargs)  # type: ignore[arg-type]

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

    def rev_parse(self, ref: str) -> str:
        """Full commit SHA of ``ref`` (validated; never an option)."""
        self._check_start_point(ref)
        return self._git("rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()

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
    def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] | None = None,
        trailers: Mapping[str, str] | None = None,
    ) -> str:
        """Stage ``paths`` (or everything) and commit on the current, non-protected branch.

        ``trailers`` (e.g. ``{"AI-Coding-Task": "<run>/<task>"}``) are appended as git
        trailers so that recovery can find commits it already made (AC-04).
        """
        if not message.strip():
            raise GitCommandError(["commit"], 1, "commit message must not be empty")
        if trailers:
            lines = []
            for key, value in trailers.items():
                if not _TRAILER_KEY.match(key) or "\n" in value or not value.strip():
                    raise GitCommandError(["commit"], 1, f"invalid trailer {key!r}")
                lines.append(f"{key}: {value.strip()}")
            message = message.rstrip() + "\n\n" + "\n".join(lines)
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

    def find_commits_by_trailer(self, key: str, value: str, *, ref: str = "--all") -> list[str]:
        """SHAs of commits carrying trailer ``key: value`` (newest first)."""
        if not _TRAILER_KEY.match(key):
            raise GitCommandError(["log"], 1, f"invalid trailer key {key!r}")
        if self.head_commit() is None:
            return []
        args = ["log", "--format=%H%x1f%(trailers:key=" + key + ",valueonly,separator=%x1d)%x1e"]
        args.append("--all" if ref == "--all" else self._verified_ref(ref))
        proc = self._git(*args)
        found: list[str] = []
        for record in proc.stdout.split("\x1e"):
            sha, _, values = record.strip("\n").partition("\x1f")
            if sha and value in {v.strip() for v in values.split("\x1d")}:
                found.append(sha)
        return found

    def _verified_ref(self, ref: str) -> str:
        self._check_start_point(ref)
        return ref

    # -------------------------------------------------------------------- push --
    def push(self, branch: str, *, remote: str = "origin", token: str | None = None) -> str:
        """Push ``branch`` (never protected, never forced); returns the pushed SHA.

        A token is passed as an HTTP header through environment-only git config
        (``GIT_CONFIG_*``), so it never appears in the process argument list.
        """
        name = validate_branch_name(branch)
        self.ensure_not_protected(name)
        validate_branch_name(remote)
        if not self.branch_exists(name):
            raise GitCommandError(["push"], 1, f"branch {name!r} does not exist")
        url = self.remote_url(remote)
        if url is None:
            raise GitCommandError(["push"], 1, f"remote {remote!r} is not configured")
        if not url.startswith("file://"):  # local mirrors are used by tests and offline setups
            validate_remote_url(url)
        env = _auth_env(token) if token else {}
        _run(
            ["push", "--quiet", "--no-verify", "--porcelain", "--", remote,
             f"refs/heads/{name}:refs/heads/{name}"],
            self._root, timeout=self._timeout, extra_env=env,
        )  # fmt: skip
        sha = self._git("rev-parse", f"refs/heads/{name}").stdout.strip()
        return sha

    def remote_branch_sha(
        self, branch: str, *, remote: str = "origin", token: str | None = None
    ) -> str | None:
        """SHA of ``branch`` on the remote (``None`` if absent) — used to probe pushes."""
        name = validate_branch_name(branch)
        validate_branch_name(remote)
        proc = _run(
            ["ls-remote", "--heads", "--", remote, f"refs/heads/{name}"],
            self._root, timeout=self._timeout, extra_env=_auth_env(token) if token else None,
        )  # fmt: skip
        line = proc.stdout.strip().splitlines()
        return line[0].split()[0] if line else None

    # ------------------------------------------------------------ diffs/patches --
    def diff(self, base: str, head: str = "HEAD", *, paths: Sequence[str] = ()) -> str:
        """Unified diff ``base..head`` (B-035)."""
        self._check_start_point(base)
        self._check_start_point(head)
        return self._git(
            "diff", "--no-color", "--no-ext-diff", f"{base}..{head}", "--", *paths
        ).stdout

    def diff_stat(self, base: str, head: str = "HEAD") -> list[tuple[str, int, int]]:
        """``(path, added, deleted)`` per changed file; binary files report ``-1``."""
        self._check_start_point(base)
        self._check_start_point(head)
        out = self._git("diff", "--numstat", "-z", "--no-ext-diff", f"{base}..{head}").stdout
        stats: list[tuple[str, int, int]] = []
        for record in (r for r in out.split("\0") if r):
            added, deleted, path = record.split("\t", 2)
            stats.append(
                (path, int(added) if added != "-" else -1, int(deleted) if deleted != "-" else -1)
            )
        return stats

    def apply_patch(self, patch: str, *, check_only: bool = False) -> None:
        """Apply a unified diff to the working tree of a non-protected branch."""
        branch = self.current_branch()
        if branch is None:
            raise ProtectedBranchError("refusing to apply a patch on a detached HEAD")
        self.ensure_not_protected(branch)
        args = ["apply", "--whitespace=nowarn", "--recount"]
        if check_only:
            args.append("--check")
        proc = _run_input([*args, "-"], self._root, patch, timeout=self._timeout)
        if proc.returncode != 0:
            raise GitCommandError(["apply"], proc.returncode, proc.stderr)

    def rollback_to(self, commit: str, *, discard_changes: bool = False) -> str:
        """Reset the current non-protected branch to ``commit`` (rollback, ТЗ §18).

        Refuses to destroy uncommitted work unless ``discard_changes`` is set.
        """
        branch = self.current_branch()
        if branch is None:
            raise ProtectedBranchError("refusing to roll back a detached HEAD")
        self.ensure_not_protected(branch)
        self._check_start_point(commit)
        if not discard_changes and not self.status().is_clean:
            raise GitCommandError(["reset"], 1, "working tree has uncommitted changes")
        self._git("reset", "--hard", "--quiet", f"{commit}^{{commit}}")
        sha = self.head_commit()
        if sha is None:  # pragma: no cover - reset to an existing commit
            raise GitCommandError(["reset"], 1, "HEAD missing after reset")
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
_SCP_URL = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$")


def validate_remote_url(url: str) -> None:
    """Accept only network transports that cannot execute local commands."""
    if not url or url.startswith("-") or any(c in url for c in "\0\n\r \t"):
        raise InvalidRefError(f"invalid repository URL: {url!r}")
    lower = url.lower()
    if lower.startswith(("https://", "ssh://")):
        if "::" in url:
            raise InvalidRefError(f"invalid repository URL: {url!r}")
        return
    if _SCP_URL.match(url):
        return
    raise InvalidRefError(
        f"unsupported repository URL {url!r}: use https://, ssh:// or user@host:path"
    )


def _auth_env(token: str) -> dict[str, str]:
    """Environment-only git config carrying a GitHub token (not visible in argv)."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    default_redactor.add_literals([token, basic])
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def _run_input(
    args: Sequence[str], cwd: Path, data: str, *, timeout: float
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    try:
        return subprocess.run(
            [_git_executable(), *_SAFETY_CONFIG, *args],
            cwd=cwd, env=env, input=data, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(args, -1, f"timed out after {timeout}s") from exc


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
