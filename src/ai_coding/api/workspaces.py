"""Workspaces (ADR-0036): each member's own clone of a project, edited in the Web UI and
in a browser terminal with Claude Code and Codex.

A workspace is ``<data>/workspaces/<project>/<user>/repo``: a clone of the project
repository made by the server (safe: the source's configuration is not copied), then
handed to the user. From then on the server only reads and writes its *files*, through
:mod:`ai_coding.api.files`; git runs in the user's container
(:mod:`ai_coding.api.workspace_broker`). Its remotes: ``origin`` — the project's own
origin (GitHub…), so the user pushes with their credentials as in VS Code — and
``project`` — the project repository, mounted read-only at ``/project``.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ai_coding.api import files
from ai_coding.api.core import (
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    detached_project,
)
from ai_coding.api.projects import ProjectService
from ai_coding.api.rbac import Permission, allowed
from ai_coding.api.workspace_broker import WorkspaceRef
from ai_coding.domain.enums import Outcome
from ai_coding.git import LocalGitRepository
from ai_coding.git.errors import GitError
from ai_coding.git.local import validate_remote_url
from ai_coding.persistence.models import User

SCOPES = ("workspace", "project")


def branch_slug(username: str) -> str:
    """The user's part of the project branches their workspace publishes."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", username).strip(".-") or "user"


class WorkspaceService(ProjectService):
    """Members' workspaces and the project files shown in the Web UI."""

    def _paths(self, project_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Path, Path]:
        repo = self.settings.workspaces_dir / str(project_id) / str(user_id) / "repo"
        return repo, self.settings.homes_dir / str(user_id)

    def _project_root(self, project_id: uuid.UUID) -> Path:
        with self.session() as s:
            return Path(detached_project(s, project_id).root_path or "")

    def workspace_info(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        with self.session() as s:
            role = self.require(s, principal, project_id, Permission.FILES_VIEW)
            root = Path(detached_project(s, project_id).root_path or "")
        repo, _ = self._paths(project_id, principal.user_id)
        return {
            "git": (root / ".git").exists(),
            "exists": (repo / ".git").exists(),
            "can_edit": allowed(role, Permission.WORKSPACE_USE),
            "terminal": "off" if self.settings.workspaces == "off" else "on",
            "branch_prefix": f"ws/{branch_slug(principal.username)}/",
        }

    def create_workspace(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        """The caller's clone of the project (idempotent)."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.WORKSPACE_USE)
        root = self._project_root(project_id)
        repo, home = self._paths(project_id, principal.user_id)
        if not (root / ".git").exists():
            raise InvalidError("the project is not a git repository: workspaces need git")
        if not (repo / ".git").exists():
            self._clone(root, repo)
            with self.session() as s:
                self.record(s, principal.username, "workspace.create", Outcome.SUCCESS,
                            project_id=project_id)  # fmt: skip
        self._prepare_home(home, principal.username)
        return self.workspace_info(principal, project_id)

    @staticmethod
    def _clone(root: Path, repo: Path) -> None:
        if repo.exists():  # a clone interrupted earlier
            shutil.rmtree(repo)
        try:
            clone = LocalGitRepository.clone(root.as_uri(), repo, depth=None,
                                             allow_file_protocol=True)  # fmt: skip
            origin = LocalGitRepository(root).remote_url("origin")
            try:
                if origin is not None:
                    validate_remote_url(origin)  # a network remote, not a server path
            except GitError:
                origin = None
            clone.set_remote("origin", origin)
            clone.set_remote("project", "/project")
        except GitError as exc:
            raise InvalidError(f"could not create the workspace: {exc}") from exc

    @staticmethod
    def _prepare_home(home: Path, username: str) -> None:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        gitconfig = home / ".gitconfig"
        if not gitconfig.exists():
            email = f"{branch_slug(username)}@users.ai-coding.invalid"
            gitconfig.write_text(
                f"[user]\n\tname = {branch_slug(username)}\n\temail = {email}\n"
                "[safe]\n\tdirectory = /project\n[init]\n\tdefaultBranch = main\n",
                encoding="utf-8",
            )

    def workspace_ref(self, principal: Principal, project_id: uuid.UUID) -> WorkspaceRef:
        """The caller's existing workspace (for the terminal and git)."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.WORKSPACE_USE)
        repo, home = self._paths(project_id, principal.user_id)
        if not (repo / ".git").exists():
            raise ConflictError("open the workspace first")
        self._prepare_home(home, principal.username)
        return WorkspaceRef(project_id, principal.user_id, principal.username,
                            self._project_root(project_id), repo, home)  # fmt: skip

    def principal_of(self, user_id: uuid.UUID) -> Principal:
        """The principal of an active user (the workspace service trusts tickets for the
        identity only, and checks the role itself)."""
        with self.session() as s:
            user = s.get(User, user_id)
            if user is None or not user.is_active:
                raise NotFoundError("user not found")
            return Principal(user.id, user.username, user.is_admin)

    # ---------------------------------------------------------------- files --
    def _files_root(
        self, principal: Principal, project_id: uuid.UUID, scope: str, *, write: bool
    ) -> Path:
        if scope not in SCOPES:
            raise InvalidError(f"scope must be one of {', '.join(SCOPES)}")
        if scope == "project":
            if write:
                raise ForbiddenError("the project's own files are changed through workspaces")
            with self.session() as s:
                self.require(s, principal, project_id, Permission.FILES_VIEW)
            return self._project_root(project_id)
        return self.workspace_ref(principal, project_id).repo

    def list_files(
        self, principal: Principal, project_id: uuid.UUID, path: str, scope: str
    ) -> list[dict[str, Any]]:
        return files.list_dir(self._files_root(principal, project_id, scope, write=False), path)

    def read_file(
        self, principal: Principal, project_id: uuid.UUID, path: str, scope: str
    ) -> dict[str, Any]:
        return files.read_file(self._files_root(principal, project_id, scope, write=False), path)

    def write_file(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        path: str,
        content: str,
        expected: str | None,
        *,
        create: bool = False,
    ) -> dict[str, Any]:
        root = self._files_root(principal, project_id, "workspace", write=True)
        version = files.write_file(root, path, content, expected=expected, create=create)
        with self.session() as s:
            self.record(s, principal.username, "workspace.file.write", Outcome.SUCCESS,
                        project_id=project_id, target=path[:300])  # fmt: skip
        return {"path": path, "version": version}

    def delete_file(self, principal: Principal, project_id: uuid.UUID, path: str) -> None:
        files.delete_file(self._files_root(principal, project_id, "workspace", write=True), path)
        with self.session() as s:
            self.record(s, principal.username, "workspace.file.delete", Outcome.SUCCESS,
                        project_id=project_id, target=path[:300])  # fmt: skip

    # -------------------------------------------------------------- publish --
    def publish_bundle(
        self, principal: Principal, project_id: uuid.UUID, bundle: bytes, branch: str
    ) -> dict[str, Any]:
        """Store the workspace branch in the project repository as
        ``ws/<user>/<branch>`` (never the project's own branches)."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.WORKSPACE_USE)
        root = self._project_root(project_id)
        target = f"ws/{branch_slug(principal.username)}/{branch}"
        uploads = self.settings.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=uploads, suffix=".bundle") as handle:
            handle.write(bundle)
            handle.flush()
            try:
                commit = LocalGitRepository(root).fetch_bundle(Path(handle.name), branch, target)
            except GitError as exc:
                raise InvalidError(f"could not publish: {exc}") from exc
        with self.session() as s:
            self.record(s, principal.username, "workspace.publish", Outcome.SUCCESS,
                        project_id=project_id, target=target,
                        details={"commit": commit})  # fmt: skip
        return {"branch": target, "commit": commit}

    def record_review(
        self, principal: Principal, project_id: uuid.UUID, action: str, path: str
    ) -> None:
        """A decision of the AI Changes Review (the path only, never the change itself)."""
        with self.session() as s:
            self.record(s, principal.username, "workspace.review", Outcome.SUCCESS,
                        project_id=project_id, target=action, details={"path": path})  # fmt: skip

    def record_terminal(self, principal: Principal, project_id: uuid.UUID, action: str) -> None:
        with self.session() as s:
            self.record(s, principal.username, action, Outcome.SUCCESS, project_id=project_id)


__all__ = ["SCOPES", "WorkspaceService", "branch_slug"]
