"""Projects (ТЗ §13): registration from a Git URL or an allowed directory, the
server-side configuration, membership and roles."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_coding.api.core import (
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    ServerCore,
    detached_project,
    iso_or_none,
)
from ai_coding.api.kinds import KINDS
from ai_coding.api.rbac import Permission, capped
from ai_coding.api.settings import ServerSettings
from ai_coding.config.generator import render_initial_config, sanitize_project_name
from ai_coding.config.loader import STATE_CONFIG, find_config, find_project_config
from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import validate_file
from ai_coding.detection import detect_stacks
from ai_coding.domain.enums import Outcome, Role
from ai_coding.git import LocalGitRepository
from ai_coding.git.errors import GitError
from ai_coding.persistence import Database
from ai_coding.persistence.db import ensure_state_dir
from ai_coding.persistence.models import (
    Project,
    ProjectMember,
    User,
)

_CONFIG_CACHE_SIZE = 256


class ProjectService(ServerCore):
    """Projects, their configuration and members."""

    def __init__(self, settings: ServerSettings, db: Database | None = None) -> None:
        super().__init__(settings, db)
        # Validated configurations by (path, mtime, size): parsing per request dominated
        # run requests under load (B-800).
        self._config_cache: dict[tuple[str, int, int], AICodingConfig] = {}
        self._config_lock = threading.Lock()

    def create_project(
        self,
        principal: Principal,
        name: str,
        *,
        git_url: str | None = None,
        branch: str | None = None,
        path: str | None = None,
    ) -> uuid.UUID:
        """Register a project; the creator becomes its Owner.

        Only instance admins may create projects. Sources: a Git URL (safe clone into
        the server data directory) or, if configured, a directory under an allowed root.
        """
        if not principal.is_admin or principal.token_project_id is not None:
            raise ForbiddenError("only instance administrators may create projects")
        clean = sanitize_project_name(name)
        if not clean or clean != name.strip():
            raise InvalidError("project name may contain letters, digits, '.', '_' and '-' only")
        if (git_url is None) == (path is None):
            raise InvalidError("give exactly one of git_url or path")
        with self.session() as s:
            if s.scalar(select(Project).where(Project.name == clean)) is not None:
                raise ConflictError(f"project '{clean}' already exists")
        project_id = uuid.uuid4()
        root = self._checkout(project_id, git_url=git_url, branch=branch, path=path)
        self._ensure_config(root, clean)
        with self.session() as s:
            project = Project(id=project_id, name=clean, root_path=str(root))
            s.add(project)
            s.flush()
            s.add(
                ProjectMember(
                    project_id=project.id, user_id=principal.user_id, role=Role.OWNER.value
                )
            )
            self.record(
                s,
                principal.username,
                "project.create",
                Outcome.SUCCESS,
                project_id=project.id,
                target=clean,
                details={"source": "git" if git_url else "path"},
            )
            try:
                s.flush()
            except IntegrityError:
                raise ConflictError(f"project '{clean}' already exists") from None
        return project_id

    def _checkout(
        self, project_id: uuid.UUID, *, git_url: str | None, branch: str | None, path: str | None
    ) -> Path:
        if path is not None:
            root = Path(path).expanduser().resolve()
            if not any(root.is_relative_to(r) for r in self.settings.allowed_roots):
                raise ForbiddenError("path is outside AI_CODING_SERVER_ALLOWED_ROOTS")
            if not root.is_dir():
                raise InvalidError("path is not a directory")
            return root
        target = self.settings.projects_dir / str(project_id)
        try:
            repo = LocalGitRepository.clone(str(git_url), target, branch=branch, depth=None)
        except GitError as exc:
            raise InvalidError(f"clone failed: {exc}") from exc
        return repo.root

    @staticmethod
    def _ensure_config(root: Path, name: str) -> None:
        """A server-side config in ``.ai-coding/`` when the repository has none."""
        if find_config(root) is not None:
            return
        ensure_state_dir(root)
        stacks = [e.stack for e in detect_stacks(root)]
        text, _ = render_initial_config(name=name, stacks=stacks)
        (root / STATE_CONFIG).write_text(text, encoding="utf-8")

    def project_config(self, project: Project) -> tuple[Path, AICodingConfig]:
        """Root and configuration of a project; the name is the server's project name."""
        root = Path(project.root_path or "")
        path = find_project_config(root) or (root / STATE_CONFIG)
        config = self._validated_config(path)
        renamed = config.project.model_copy(update={"name": project.name})
        return root, config.model_copy(update={"project": renamed})

    def _validated_config(self, path: Path) -> AICodingConfig:
        """Parse and validate ``path`` once per version of the file.

        YAML parsing and JSON Schema validation dominated every run request under load
        (B-800); the key includes mtime and size, so an edited file is read again.
        """
        try:
            stat = path.stat()
            key: tuple[str, int, int] | None = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        with self._config_lock:
            cached = self._config_cache.get(key) if key else None
        if cached is not None:
            return cached
        report = validate_file(path)
        if not report.ok or report.config is None:
            raise InvalidError(f"the project configuration is invalid ({path.name})")
        if key is not None:
            with self._config_lock:
                if len(self._config_cache) >= _CONFIG_CACHE_SIZE:
                    self._config_cache.pop(next(iter(self._config_cache)))
                self._config_cache[key] = report.config
        return report.config

    def list_projects(self, principal: Principal) -> list[dict[str, Any]]:
        """Projects the caller is a member of (admins also see the others' names)."""
        with self.session() as s:
            rows = s.execute(
                select(Project, ProjectMember.role)
                .outerjoin(
                    ProjectMember,
                    (ProjectMember.project_id == Project.id)
                    & (ProjectMember.user_id == principal.user_id),
                )
                .where(Project.deleted_at.is_(None))
                .order_by(Project.name)
            ).all()
            out = []
            for project, member_role in rows:
                role: str | None = member_role  # NULL when not a member (outer join)
                scoped_out = principal.token_project_id not in (None, project.id)
                if scoped_out or (role is None and not principal.is_admin):
                    continue
                effective = capped(Role(role), principal.token_max_role) if role else None
                out.append(
                    {
                        "id": str(project.id),
                        "name": project.name,
                        "role": effective.value if effective else None,
                        "created_at": iso_or_none(project.created_at),
                    }
                )
            return out

    def get_project(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        with self.session() as s:
            role = self.require(s, principal, project_id, Permission.PROJECT_VIEW)
            project = detached_project(s, project_id)
            members = s.execute(
                select(User.username, ProjectMember.role)
                .join(
                    ProjectMember,
                    ProjectMember.user_id == User.id,
                )
                .where(ProjectMember.project_id == project_id)
                .order_by(User.username)
            ).all()
            return {
                "id": str(project.id),
                "name": project.name,
                "role": role.value,
                "created_at": iso_or_none(project.created_at),
                "members": [{"username": u, "role": r} for u, r in members],
                "kinds": sorted(KINDS),
            }

    def set_member(
        self, principal: Principal, project_id: uuid.UUID, username: str, role: Role | None
    ) -> None:
        """Add, change (``role``) or remove (``None``) a member. Owner only."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.MEMBERS_MANAGE)
            user = s.scalar(select(User).where(User.username == username))
            if user is None:
                raise NotFoundError(f"user '{username}' not found")
            member = s.get(ProjectMember, (project_id, user.id))
            owners = s.scalars(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id, ProjectMember.role == Role.OWNER.value
                )
            ).all()
            if (
                member is not None
                and member.role == Role.OWNER.value
                and len(owners) == 1
                and role is not Role.OWNER
            ):
                raise ConflictError("a project must keep at least one owner")
            if role is None:
                if member is not None:
                    s.delete(member)
            elif member is None:
                s.add(ProjectMember(project_id=project_id, user_id=user.id, role=role.value))
            else:
                member.role = role.value
            self.record(
                s,
                principal.username,
                "project.member",
                Outcome.SUCCESS,
                project_id=project_id,
                target=username,
                details={"role": role.value if role else None},
            )
