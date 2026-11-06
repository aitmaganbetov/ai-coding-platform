"""Projects (ТЗ §13): registration from a Git URL or an allowed directory, the
server-side configuration, membership and roles."""

from __future__ import annotations

import itertools
import re
import shutil
import tarfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel
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
from ai_coding.persistence.db import STATE_DIR, ensure_state_dir
from ai_coding.persistence.models import (
    Project,
    ProjectMember,
    User,
)

_CONFIG_CACHE_SIZE = 256
_MAX_SOURCE_SCAN = 2000  # entries read per allowed root
_MAX_SOURCE_DIRS = 500  # directories offered per allowed root
_MAX_UPLOAD_ENTRIES = 100_000  # files and directories in one uploaded folder


# Free-text gate settings: a command line may embed a credential (an index URL with a
# token), so the configuration summary leaves them out.
_GATE_FREE_TEXT = frozenset({"command", "install", "image"})


def _gate_summary(gate: BaseModel) -> dict[str, Any]:
    """``enabled``, ``blocking`` and the thresholds of one Quality Gate."""
    return {
        key: value
        for key, value in gate.model_dump(mode="json").items()
        if isinstance(value, bool | int | float | str) and key not in _GATE_FREE_TEXT
    }


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _source_entry(
    path: Path, name: str, used: list[Path], visible: dict[str, str | None]
) -> dict[str, Any]:
    return {
        "name": name,
        "path": str(path),
        "git": (path / ".git").exists(),
        "used": any(_overlaps(path, u) for u in used),
        "project": visible.get(str(path)),
    }


def _wanted(member: tarfile.TarInfo) -> bool:
    """Regular files and directories only; never a ``.git`` (directory or gitlink file:
    its config could make git run commands) nor the uploader's local ``.ai-coding/``
    state, except the configuration in it."""
    path = PurePosixPath(member.name)
    if not path.parts or ".git" in path.parts or member.issparse():
        return False
    if not (member.isfile() or member.isdir()):
        return False
    return path.parts[0] != STATE_DIR or path == PurePosixPath(STATE_CONFIG)


def _extract_upload(archive: Path, target: Path) -> tuple[int, int]:
    """Extract an uploaded uncompressed tar into the new directory ``target``; returns
    (files, bytes). The ``data`` filter refuses absolute paths, ``..`` and anything that
    would land outside ``target``; the archive is not compressed, so its contents are
    bounded by the upload limit."""
    target.mkdir(parents=True)
    try:
        with tarfile.open(archive, mode="r:") as tar:
            members: list[tarfile.TarInfo] = []
            for member in tar:
                if not _wanted(member):
                    continue
                if len(members) >= _MAX_UPLOAD_ENTRIES:
                    raise InvalidError(f"the folder has more than {_MAX_UPLOAD_ENTRIES} entries")
                members.append(member)
            files = [m for m in members if m.isfile()]
            if not files:
                raise InvalidError("the uploaded folder contains no files")
            tar.extractall(target, members=members, filter="data")
    except tarfile.FilterError as exc:  # the message would name server paths: not shown
        raise InvalidError(f"unsafe entry in the upload: {exc.tarinfo.name!r}") from None
    except (tarfile.TarError, OSError):
        raise InvalidError("the upload is not a valid uncompressed tar archive") from None
    return len(files), sum(m.size for m in files)


class ProjectService(ServerCore):
    """Projects, their configuration and members."""

    def __init__(self, settings: ServerSettings, db: Database | None = None) -> None:
        super().__init__(settings, db)
        # Validated configurations by (path, mtime, size): parsing per request dominated
        # run requests under load (B-800).
        self._config_cache: dict[tuple[str, int, int], AICodingConfig] = {}
        self._config_lock = threading.Lock()

    # ------------------------------------------------------------ creation --
    def can_create_projects(self, principal: Principal) -> bool:
        """Every user (``AI_CODING_SERVER_PROJECT_CREATORS=all``, the default) or only
        instance admins; never with a project-scoped or role-capped token."""
        if principal.token_project_id is not None:
            return False
        if principal.token_max_role not in (None, Role.OWNER):
            return False
        return self.settings.project_creators == "all" or principal.is_admin

    def _require_creator(self, principal: Principal) -> None:
        if not self.can_create_projects(principal):
            self._denied(principal.username, "access.project.create")
            raise ForbiddenError("you may not create projects")

    def check_new_project(self, principal: Principal, name: str) -> None:
        """Refuse up front what a creation would refuse (before an upload is received)."""
        self._require_creator(principal)
        self._free_name(name)

    def _free_name(self, name: str) -> str:
        clean = sanitize_project_name(name)
        if not clean or clean != name.strip():
            raise InvalidError("project name may contain letters, digits, '.', '_' and '-' only")
        with self.session() as s:
            if s.scalar(select(Project).where(Project.name == clean)) is not None:
                raise ConflictError(f"project '{clean}' already exists")
        return clean

    def create_project(
        self,
        principal: Principal,
        name: str,
        *,
        git_url: str | None = None,
        branch: str | None = None,
        path: str | None = None,
        git_token: str | None = None,
    ) -> uuid.UUID:
        """Register a project; the creator becomes its Owner.

        Sources: a Git URL (safe clone into the server data directory; ``git_token``
        opens a private HTTPS repository and is never stored) or a directory under an
        allowed root that no other project uses.
        """
        self._require_creator(principal)
        if (git_url is None) == (path is None):
            raise InvalidError("give exactly one of git_url or path")
        if git_token is not None and git_url is None:
            raise InvalidError("an access token is only used to clone a git_url")
        clean = self._free_name(name)
        project_id = uuid.uuid4()
        if path is not None:
            root = self._connect_directory(path)
            return self._register(principal, project_id, clean, root, {"source": "path"})
        target = self.settings.projects_dir / str(project_id)

        def build() -> uuid.UUID:
            try:
                repo = LocalGitRepository.clone(
                    str(git_url), target, branch=branch, depth=None, token=git_token
                )
            except GitError as exc:
                raise InvalidError(f"clone failed: {exc}") from exc
            details = {"source": "git", "private": git_token is not None}
            return self._register(principal, project_id, clean, repo.root, details)

        return self._managed(target, build)

    def create_uploaded_project(self, principal: Principal, name: str, archive: Path) -> uuid.UUID:
        """Register a project from a folder uploaded as an uncompressed tar archive.

        The files become the first commit of a new repository on ``main`` (an uploaded
        ``.git`` is dropped: its configuration could make the server run commands).
        """
        self._require_creator(principal)
        clean = self._free_name(name)
        project_id = uuid.uuid4()
        target = self.settings.projects_dir / str(project_id)

        def build() -> uuid.UUID:
            files, size = _extract_upload(archive, target)
            repo = LocalGitRepository.init(target, initial_branch="main", timeout=600.0)
            local = re.sub(r"[^A-Za-z0-9._-]", "_", principal.username)
            email = f"{local}@users.ai-coding.invalid"
            try:
                repo.commit_imported_tree(f"Import {clean} uploaded via the Web UI",
                                          author=(principal.username, email))  # fmt: skip
            except GitError as exc:
                raise InvalidError(f"could not commit the uploaded files: {exc}") from exc
            details = {"source": "upload", "files": files, "bytes": size}
            return self._register(principal, project_id, clean, repo.root, details)

        return self._managed(target, build)

    @staticmethod
    def _managed(target: Path, build: Callable[[], uuid.UUID]) -> uuid.UUID:
        """Run ``build``; on failure remove the directory it was filling."""
        try:
            return build()
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _register(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        name: str,
        root: Path,
        details: dict[str, Any],
    ) -> uuid.UUID:
        self._ensure_config(root, name)
        with self.session() as s:
            project = Project(id=project_id, name=name, root_path=str(root))
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
                target=name,
                details=details,
            )
            try:
                s.flush()
            except IntegrityError:
                raise ConflictError(f"project '{name}' already exists") from None
        return project_id

    def _connect_directory(self, path: str) -> Path:
        """A directory under an allowed root that no other project's directory equals,
        contains or lies in: a second project on the same files would expose them to
        the second project's members."""
        root = Path(path).expanduser().resolve()
        if not any(root.is_relative_to(r) for r in self.settings.allowed_roots):
            raise ForbiddenError("path is outside AI_CODING_SERVER_ALLOWED_ROOTS")
        if not root.is_dir():
            raise InvalidError("path is not a directory")
        if any(_overlaps(root, Path(p)) for p in self._project_roots().values()):
            raise ConflictError("this directory is already used by another project")
        return root

    def _project_roots(self) -> dict[uuid.UUID, str]:
        with self.session() as s:
            rows = s.execute(
                select(Project.id, Project.root_path).where(Project.deleted_at.is_(None))
            ).all()
        return {pid: root for pid, root in rows if root}

    def project_sources(self, principal: Principal) -> dict[str, Any]:
        """How the caller can register a project: Git, an upload (with its size limit)
        and the directories directly under the allowed roots. A directory used by a
        project is marked ``used``; the project's name is shown only to its members and
        to administrators."""
        self._require_creator(principal)
        roots = self._project_roots()
        with self.session() as s:
            names = {row.id: row.name for row in s.execute(select(Project.id, Project.name))}
            mine = set(
                s.scalars(
                    select(ProjectMember.project_id).where(
                        ProjectMember.user_id == principal.user_id
                    )
                )
            )
        visible = {
            root: names.get(pid) for pid, root in roots.items() if principal.is_admin or pid in mine
        }
        used = [Path(r) for r in roots.values()]
        return {
            "git": True,
            "upload_max_bytes": self.settings.max_upload_bytes,
            "roots": [self._source_root(r, used, visible) for r in self.settings.allowed_roots],
        }

    def _source_root(
        self, root: Path, used: list[Path], visible: dict[str, str | None]
    ) -> dict[str, Any]:
        try:  # bounded: a root with a huge number of entries is listed partially
            children = sorted(itertools.islice(root.iterdir(), _MAX_SOURCE_SCAN))
        except OSError:
            children = []
        directories: list[dict[str, Any]] = []
        for child in children:
            if len(directories) >= _MAX_SOURCE_DIRS:
                break
            if child.name.startswith("."):
                continue
            try:
                resolved = child.resolve()
                if not resolved.is_dir():
                    continue
            except OSError:
                continue
            # The same rule as registration: a symlink must not lead out of the roots.
            if not any(resolved.is_relative_to(r) for r in self.settings.allowed_roots):
                continue
            directories.append(_source_entry(resolved, child.name, used, visible))
        return {**_source_entry(root, str(root), used, visible), "directories": directories}

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
            found = {
                "id": str(project.id),
                "name": project.name,
                "role": role.value,
                "created_at": iso_or_none(project.created_at),
                "members": [{"username": u, "role": r} for u, r in members],
                "kinds": sorted(KINDS),
            }
        return {**found, "config": self.config_summary(project)}

    def config_summary(self, project: Project) -> dict[str, Any] | None:
        """What the project page shows of the validated configuration, or ``None`` when
        it cannot be read or is invalid (the project stays reachable to be fixed).

        Only settings, never what could carry a secret: no key variable names, base URLs,
        commands or images.
        """
        try:
            _root, config = self.project_config(project)
        except Exception:  # an unreadable or invalid file must not hide the project
            return None
        ai, budgets = config.ai, config.budgets
        return {
            "stacks": [s.value for s in config.stacks],
            "mode": config.mode.value,
            "execution": {"backend": config.execution.backend.value,
                          "network": config.execution.docker.network},
            "ai": {"enabled": ai.enabled, "provider": ai.provider.value, "model": ai.model},
            "security": {"network": config.security.network.value,
                         "egress_allowlist": list(config.security.egress_allowlist)},
            "budgets": {"max_tokens_per_run": budgets.max_tokens_per_run,
                        "max_cost_usd_per_run": budgets.max_cost_usd_per_run,
                        "max_duration_minutes": budgets.max_duration_minutes},
            "quality_gates": {name: _gate_summary(gate) for name, gate in config.quality_gates},
        }  # fmt: skip

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
