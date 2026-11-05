"""Server services: authentication, project-scoped RBAC, projects, runs, results.

Every data access goes through :meth:`Server.require`, which resolves the caller's role
in the project (capped by the token's scope) and raises :class:`NotFoundError` when the
caller is not a member — a resource of another project is indistinguishable from a
missing one, so nothing leaks across projects (AC-08). Each operation is recorded
in the append-only audit trail with actor, project, run and outcome (ТЗ §3).
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_coding.api.kinds import KINDS, build_kind
from ai_coding.api.rbac import Permission, allowed, capped
from ai_coding.api.security import (
    hash_password,
    looks_like_token,
    needs_rehash,
    new_token,
    token_hash,
    verify_password,
)
from ai_coding.api.settings import ServerSettings
from ai_coding.config.generator import render_initial_config, sanitize_project_name
from ai_coding.config.loader import find_config
from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import validate_file
from ai_coding.detection import detect_stacks
from ai_coding.domain.enums import Outcome, Role
from ai_coding.git import LocalGitRepository
from ai_coding.git.errors import GitError
from ai_coding.orchestrator import HandlerRegistry, Orchestrator, RunOptions, TaskPlanner
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.orchestrator.errors import OrchestratorError
from ai_coding.persistence import Database, upgrade_to_head
from ai_coding.persistence.db import STATE_DIR, ensure_state_dir
from ai_coding.persistence.models import (
    ApiToken,
    Artifact,
    AuditEvent,
    Credential,
    Finding,
    IdempotencyRecord,
    Project,
    ProjectMember,
    Run,
    RunJob,
    User,
)
from ai_coding.persistence.types import utcnow

SERVER_CONFIG = "config.yml"  # <checkout>/.ai-coding/config.yml when the repo has none
_DUMMY_HASH = hash_password("dummy-password-for-timing")
_LAST_USED_GRANULARITY = timedelta(minutes=1)
_KEY = re.compile(r"[\x21-\x7e]{1,200}")


# ------------------------------------------------------------------- errors --
class ApiError(Exception):
    """Error with an HTTP status; ``message`` is safe to show to the caller."""

    status = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnauthorizedError(ApiError):
    status, code = 401, "unauthorized"


class ForbiddenError(ApiError):
    status, code = 403, "forbidden"


class NotFoundError(ApiError):
    status, code = 404, "not_found"


class ConflictError(ApiError):
    status, code = 409, "conflict"


class InvalidError(ApiError):
    status, code = 422, "invalid"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    user_id: uuid.UUID
    username: str
    is_admin: bool
    token_id: uuid.UUID | None = None
    token_project_id: uuid.UUID | None = None
    token_max_role: Role | None = None


# ------------------------------------------------------------------- server --
class Server:
    """Facade used by the HTTP layer, the CLI and the workers."""

    def __init__(self, settings: ServerSettings, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db or Database(settings.database_url)
        self.audit = AuditLogger()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def close(self) -> None:
        """Release database connections (orchestrators share this database)."""
        self.db.dispose()

    def migrate(self) -> None:
        """Create/upgrade the schema."""
        upgrade_to_head(self.db)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.db.session() as session:
            yield session

    def _audit(self, s: Session, actor: str, action: str, outcome: Outcome, **kw: Any) -> None:
        self.audit.record(s, actor=actor, action=action, outcome=outcome, **kw)

    def _denied(self, actor: str, action: str, **kw: Any) -> None:
        """Record a refusal in its own transaction: the request's transaction is rolled
        back by the error that follows, but the denial must stay in the audit trail."""
        with self.session() as s:
            self.audit.record(s, actor=actor, action=action, outcome=Outcome.DENIED, **kw)

    # ------------------------------------------------------- idempotency --
    def idempotent(
        self,
        principal: Principal,
        key: str,
        *,
        method: str,
        path: str,
        body: bytes,
        produce: Callable[[], tuple[int, dict[str, Any]]],
    ) -> tuple[int, dict[str, Any], bool]:
        """Run ``produce`` once per ``(user, Idempotency-Key)``; replay its response.

        Reusing a key for a different method, path or body is refused (422). Only
        successful responses are stored, so a failed request can be retried.
        Returns ``(status, payload, replayed)``.
        """
        if not _KEY.fullmatch(key):
            raise InvalidError("Idempotency-Key must be 1-200 visible ASCII characters")
        digest = hashlib.sha256(body).hexdigest()
        stored = self._replay(principal, key, method, path, digest)
        if stored is not None:
            return stored[0], stored[1], True
        status, payload = produce()
        try:
            with self.session() as s:
                s.add(
                    IdempotencyRecord(
                        user_id=principal.user_id,
                        key=key,
                        method=method,
                        path=path[:500],
                        body_sha256=digest,
                        status_code=status,
                        response=payload,
                    )
                )
        except IntegrityError:  # a concurrent request with the same key won the race
            stored = self._replay(principal, key, method, path, digest)
            if stored is not None:
                return stored[0], stored[1], True
        return status, payload, False

    def _replay(
        self, principal: Principal, key: str, method: str, path: str, digest: str
    ) -> tuple[int, dict[str, Any]] | None:
        with self.session() as s:
            rec = s.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.user_id == principal.user_id, IdempotencyRecord.key == key
                )
            )
            if rec is None:
                return None
            if (rec.method, rec.path, rec.body_sha256) != (method, path[:500], digest):
                raise InvalidError("Idempotency-Key was already used for a different request")
            return rec.status_code, dict(rec.response)

    # ------------------------------------------------------------- users --
    def create_user(
        self,
        username: str,
        password: str,
        *,
        display_name: str | None = None,
        is_admin: bool = False,
        actor: str = "system",
    ) -> uuid.UUID:
        """Create a user with a password (argon2id)."""
        name = username.strip()
        if not name or len(name) > 150 or any(c.isspace() for c in name):
            raise InvalidError("username must be 1-150 characters without whitespace")
        hashed = hash_password(password)
        with self.session() as s:
            if s.scalar(select(User).where(User.username == name)) is not None:
                raise ConflictError(f"user '{name}' already exists")
            user = User(username=name, display_name=display_name or name, is_admin=is_admin)
            s.add(user)
            s.flush()
            s.add(Credential(user_id=user.id, password_hash=hashed))
            self._audit(
                s,
                actor,
                "user.create",
                Outcome.SUCCESS,
                target=name,
                details={"is_admin": is_admin},
            )
            return user.id

    def set_password(self, username: str, password: str, *, actor: str = "system") -> None:
        """Replace a user's password."""
        hashed = hash_password(password)
        with self.session() as s:
            user = s.scalar(select(User).where(User.username == username))
            if user is None:
                raise NotFoundError(f"user '{username}' not found")
            cred = s.get(Credential, user.id)
            if cred is None:
                s.add(Credential(user_id=user.id, password_hash=hashed))
            else:
                cred.password_hash = hashed
            self._audit(s, actor, "user.password", Outcome.SUCCESS, target=username)

    def login(self, username: str, password: str) -> Principal | None:
        """Verify a password; ``None`` on failure (same timing for unknown users)."""
        with self.session() as s:
            user = s.scalar(select(User).where(User.username == username))
            cred = s.get(Credential, user.id) if user is not None else None
            ok = verify_password(cred.password_hash if cred else _DUMMY_HASH, password)
            if not (ok and user is not None and cred is not None and user.is_active):
                self._audit(s, username[:150] or "-", "auth.login", Outcome.DENIED)
                return None
            if needs_rehash(cred.password_hash):
                cred.password_hash = hash_password(password)
            self._audit(s, user.username, "auth.login", Outcome.SUCCESS)
            return Principal(user.id, user.username, user.is_admin)

    # ------------------------------------------------------------ tokens --
    def issue_token(
        self,
        principal: Principal,
        name: str,
        *,
        project_id: uuid.UUID | None = None,
        max_role: Role | None = None,
        ttl_hours: int | None = None,
    ) -> tuple[str, uuid.UUID]:
        """New API token for the caller (the clear token is returned once).

        A token cannot have more rights than the caller: when scoped to a project the
        caller must be a member, and its role is capped. ``agent`` tokens must be
        project-scoped and short-lived (ТЗ §3).
        """
        if principal.token_project_id is not None:
            raise ForbiddenError("a project-scoped token cannot create tokens")
        default_hours = self.settings.token_ttl_days * 24
        hours = ttl_hours or default_hours
        if max_role is Role.AGENT:
            if project_id is None:
                raise InvalidError("agent tokens must be limited to one project")
            hours = min(
                ttl_hours or self.settings.agent_token_ttl_hours,
                self.settings.agent_token_ttl_hours,
            )
        if not 1 <= hours <= 24 * 365:
            raise InvalidError("token lifetime must be between 1 hour and 365 days")
        token = new_token()
        with self.session() as s:
            if project_id is not None and self._role(s, principal, project_id) is None:
                raise NotFoundError("project not found")
            row = ApiToken(
                user_id=principal.user_id,
                name=name[:100] or "token",
                token_hash=token_hash(token),
                project_id=project_id,
                max_role=max_role.value if max_role else None,
                expires_at=utcnow() + timedelta(hours=hours),
            )
            s.add(row)
            s.flush()
            self._audit(
                s,
                principal.username,
                "token.issue",
                Outcome.SUCCESS,
                project_id=project_id,
                target=str(row.id),
                details={"max_role": row.max_role, "hours": hours},
            )
            return token, row.id

    def authenticate(self, token: str) -> Principal | None:
        """Principal of a valid, unexpired, unrevoked token of an active user."""
        if not looks_like_token(token):
            return None
        now = utcnow()
        with self.session() as s:
            row = s.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(token)))
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                return None
            user = s.get(User, row.user_id)
            if user is None or not user.is_active:
                return None
            if row.last_used_at is None or now - row.last_used_at > _LAST_USED_GRANULARITY:
                row.last_used_at = now
            return Principal(
                user.id,
                user.username,
                user.is_admin,
                row.id,
                row.project_id,
                Role(row.max_role) if row.max_role else None,
            )

    def list_tokens(self, principal: Principal) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(
                select(ApiToken)
                .where(ApiToken.user_id == principal.user_id)
                .order_by(ApiToken.created_at)
            )
            return [
                {
                    "id": str(r.id),
                    "name": r.name,
                    "project_id": _str(r.project_id),
                    "max_role": r.max_role,
                    "created_at": _iso(r.created_at),
                    "expires_at": _iso(r.expires_at),
                    "last_used_at": _iso(r.last_used_at),
                    "revoked": r.revoked_at is not None,
                }
                for r in rows
            ]

    def revoke_token(self, principal: Principal, token_id: uuid.UUID) -> None:
        """Revoke one of the caller's tokens (admins may revoke any)."""
        with self.session() as s:
            row = s.get(ApiToken, token_id)
            if row is None or (row.user_id != principal.user_id and not principal.is_admin):
                raise NotFoundError("token not found")
            row.revoked_at = row.revoked_at or utcnow()
            self._audit(
                s,
                principal.username,
                "token.revoke",
                Outcome.SUCCESS,
                project_id=row.project_id,
                target=str(token_id),
            )

    def rotate_token(self, principal: Principal, token_id: uuid.UUID) -> tuple[str, uuid.UUID]:
        """Replace a token by a new one with the same scope; the old one is revoked."""
        with self.session() as s:
            row = s.get(ApiToken, token_id)
            if row is None or row.user_id != principal.user_id or row.revoked_at is not None:
                raise NotFoundError("token not found")
            name, project, role = row.name, row.project_id, row.max_role
            hours = (
                max(1, int((row.expires_at - row.created_at).total_seconds() // 3600))
                if row.expires_at
                else None
            )
        owner = Principal(principal.user_id, principal.username, principal.is_admin)
        token, new_id = self.issue_token(
            owner, name, project_id=project, max_role=Role(role) if role else None, ttl_hours=hours
        )
        self.revoke_token(principal, token_id)
        return token, new_id

    # ------------------------------------------------------------ access --
    def _role(self, s: Session, principal: Principal, project_id: uuid.UUID) -> Role | None:
        if principal.token_project_id is not None and principal.token_project_id != project_id:
            return None
        member = s.get(ProjectMember, (project_id, principal.user_id))
        project = s.get(Project, project_id)
        if member is None or project is None or project.deleted_at is not None:
            return None
        return capped(Role(member.role), principal.token_max_role)

    def require(
        self, s: Session, principal: Principal, project_id: uuid.UUID, permission: Permission
    ) -> Role:
        """Role of the caller if it grants ``permission``.

        Not a member (or another project's token) → :class:`NotFoundError`; a member
        without the permission → :class:`ForbiddenError` (recorded as denied).
        """
        role = self._role(s, principal, project_id)
        if role is None:
            raise NotFoundError("project not found")
        if not allowed(role, permission):
            self._denied(
                principal.username,
                f"access.{permission.value}",
                project_id=project_id,
                actor_role=role.value,
            )
            raise ForbiddenError(f"role '{role.value}' may not {permission.value}")
        return role

    def _run(
        self, s: Session, principal: Principal, run_id: uuid.UUID, permission: Permission
    ) -> tuple[Run, Role]:
        run = s.get(Run, run_id)
        if run is None:
            raise NotFoundError("run not found")
        try:
            role = self.require(s, principal, run.project_id, permission)
        except NotFoundError:
            raise NotFoundError("run not found") from None
        return run, role

    # ---------------------------------------------------------- projects --
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
            self._audit(
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
        state = ensure_state_dir(root)
        stacks = [e.stack for e in detect_stacks(root)]
        text, _ = render_initial_config(name=name, stacks=stacks)
        (state / SERVER_CONFIG).write_text(text, encoding="utf-8")

    def project_config(self, project: Project) -> tuple[Path, AICodingConfig]:
        """Root and configuration of a project; the name is the server's project name."""
        root = Path(project.root_path or "")
        path = find_config(root) or (root / STATE_DIR / SERVER_CONFIG)
        report = validate_file(path)
        if not report.ok or report.config is None:
            raise InvalidError(f"the project configuration is invalid ({path.name})")
        config = report.config
        renamed = config.project.model_copy(update={"name": project.name})
        return root, config.model_copy(update={"project": renamed})

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
                        "created_at": _iso(project.created_at),
                    }
                )
            return out

    def get_project(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        with self.session() as s:
            role = self.require(s, principal, project_id, Permission.PROJECT_VIEW)
            project = _detached(s, project_id)
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
                "created_at": _iso(project.created_at),
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
            self._audit(
                s,
                principal.username,
                "project.member",
                Outcome.SUCCESS,
                project_id=project_id,
                target=username,
                details={"role": role.value if role else None},
            )

    # -------------------------------------------------------------- runs --
    def orchestrator(
        self, project: Project, registry: HandlerRegistry | None = None, *, actor: str
    ) -> tuple[Orchestrator, AICodingConfig]:
        root, config = self.project_config(project)
        orch = Orchestrator(
            db=self.db,
            config=config,
            project_root=root,
            registry=registry or HandlerRegistry([]),
            actor=actor,
            worker_id=f"{self.worker_id}:{uuid.uuid4().hex[:6]}",
            local_owner=False,
        )
        return orch, config

    def create_run(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        kind: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> uuid.UUID:
        """Plan a run of ``kind`` and queue it for the workers."""
        spec = KINDS.get(kind)
        if spec is None:
            raise InvalidError(f"unknown run kind '{kind}' (known: {', '.join(sorted(KINDS))})")
        with self.session() as s:
            self.require(s, principal, project_id, spec.permission)
            project = _detached(s, project_id)
        orch, config = self.orchestrator(project, actor=principal.username)
        try:
            (_registry, steps), fixed = build_kind(kind, orch.project_root, config, params)
        except (ValueError, KeyError) as exc:
            raise InvalidError(f"invalid parameters: {exc}") from exc
        try:
            run_id = orch.create_run(
                TaskPlanner().build(steps),
                mode=spec.mode,
                goal=_goal(kind, fixed),
                idempotency_key=idempotency_key,
                options=RunOptions(extra={"kind": kind, "params": fixed}),
            )
        except OrchestratorError as exc:
            raise ConflictError(str(exc)) from exc
        with self.session() as s:
            exists = s.scalar(select(RunJob).where(RunJob.run_id == run_id))
            if exists is None:
                s.add(RunJob(run_id=run_id, kind="execute", state="queued"))
            self._audit(
                s,
                principal.username,
                "run.create",
                Outcome.SUCCESS,
                project_id=project_id,
                run_id=run_id,
                target=kind,
            )
        return run_id

    def run_status(self, principal: Principal, run_id: uuid.UUID) -> dict[str, Any]:
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.RUN_VIEW)
            project = _detached(s, run.project_id)
            jobs = [
                {"kind": j.kind, "state": j.state, "attempts": j.attempts, "error": j.error}
                for j in s.scalars(
                    select(RunJob).where(RunJob.run_id == run_id).order_by(RunJob.created_at)
                )
            ]
            kind = run.options.get("kind")
        orch, _ = self.orchestrator(project, actor=principal.username)
        status = orch.status(run_id)
        return {**status.to_dict(), "project_id": str(project.id), "kind": kind, "jobs": jobs}

    def list_runs(
        self, principal: Principal, project_id: uuid.UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
            rows = s.scalars(
                select(Run)
                .where(Run.project_id == project_id)
                .order_by(Run.created_at.desc())
                .limit(min(limit, 200))
            )
            return [
                {
                    "id": str(r.id),
                    "state": r.state,
                    "mode": r.mode,
                    "goal": r.goal,
                    "kind": r.options.get("kind"),
                    "requested_by": r.requested_by,
                    "created_at": _iso(r.created_at),
                    "finished_at": _iso(r.finished_at),
                }
                for r in rows
            ]

    def decide(
        self,
        principal: Principal,
        run_id: uuid.UUID,
        *,
        approve: bool,
        approval_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Approve or reject pending approvals, then queue the run to continue."""
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.RUN_APPROVE)
            project = _detached(s, run.project_id)
        orch, _ = self.orchestrator(project, actor=principal.username)
        try:
            decided = orch.decide(
                run_id,
                approve=approve,
                approval_id=approval_id,
                note=note,
                actor=principal.username,
            )
        except OrchestratorError as exc:
            raise ConflictError(str(exc)) from exc
        status = orch.status(run_id)
        if not [a for a in status.approvals if a["status"] == "pending"]:
            self._enqueue(run_id, "resume")
        return {"decided": [str(a.id) for a in decided], "state": status.state.value}

    def cancel(self, principal: Principal, run_id: uuid.UUID, reason: str) -> dict[str, Any]:
        with self.session() as s:
            run, role = self._run(s, principal, run_id, Permission.RUN_CANCEL_OWN)
            if (
                not allowed(role, Permission.RUN_CANCEL_ANY)
                and run.requested_by != principal.username
            ):
                self._denied(
                    principal.username, "run.cancel", project_id=run.project_id, run_id=run_id
                )
                raise ForbiddenError("developers may cancel only their own runs")
            project = _detached(s, run.project_id)
        orch, _ = self.orchestrator(project, actor=principal.username)
        try:
            status = orch.cancel(run_id, reason=reason[:500] or "cancelled via API")
        except OrchestratorError as exc:
            raise ConflictError(str(exc)) from exc
        with self.session() as s:
            for job in s.scalars(
                select(RunJob).where(RunJob.run_id == run_id, RunJob.state == "queued")
            ):
                job.state, job.finished_at = "done", utcnow()
        return {"state": status.state.value}

    def _enqueue(self, run_id: uuid.UUID, kind: str) -> None:
        with self.session() as s:
            busy = s.scalar(
                select(RunJob).where(
                    RunJob.run_id == run_id, RunJob.state.in_(("queued", "leased"))
                )
            )
            if busy is None:
                s.add(RunJob(run_id=run_id, kind=kind, state="queued"))

    def artifacts(self, principal: Principal, run_id: uuid.UUID) -> list[dict[str, Any]]:
        with self.session() as s:
            self._run(s, principal, run_id, Permission.ARTIFACTS_VIEW)
            rows = s.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
            )
            return [
                {
                    "id": str(a.id),
                    "name": a.name,
                    "sha256": a.sha256,
                    "media_type": a.media_type,
                    "size_bytes": a.size_bytes,
                    "created_at": _iso(a.created_at),
                }
                for a in rows
            ]

    def artifact_content(
        self, principal: Principal, run_id: uuid.UUID, artifact_id: uuid.UUID
    ) -> tuple[str, str, bytes]:
        """``(name, media type, bytes)`` of one artifact of the run."""
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.ARTIFACTS_VIEW)
            art = s.get(Artifact, artifact_id)
            if art is None or art.run_id != run_id:
                raise NotFoundError("artifact not found")
            project = _detached(s, run.project_id)
            name, media, digest = art.name, art.media_type, art.sha256
        orch, _ = self.orchestrator(project, actor=principal.username)
        return name, media, orch.artifacts.get(digest)

    def findings(
        self, principal: Principal, project_id: uuid.UUID, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.FINDINGS_VIEW)
            rows = s.scalars(
                select(Finding)
                .where(Finding.project_id == project_id)
                .order_by(Finding.created_at.desc())
                .limit(min(limit, 2000))
            )
            return [_finding_dict(f) for f in rows]

    def audit_events(
        self, principal: Principal, project_id: uuid.UUID | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Audit trail of one project, or of every project the caller may audit."""
        with self.session() as s:
            if project_id is not None:
                self.require(s, principal, project_id, Permission.AUDIT_VIEW)
                projects = [project_id]
            else:
                members = s.scalars(
                    select(ProjectMember).where(ProjectMember.user_id == principal.user_id)
                ).all()
                projects = [
                    m.project_id
                    for m in members
                    if allowed(self._role(s, principal, m.project_id), Permission.AUDIT_VIEW)
                ]
            if not projects:
                return []
            rows = s.scalars(
                select(AuditEvent)
                .where(AuditEvent.project_id.in_(projects))
                .order_by(AuditEvent.created_at.desc())
                .limit(min(limit, 1000))
            )
            return [
                {
                    "id": str(e.id),
                    "created_at": _iso(e.created_at),
                    "actor": e.actor,
                    "actor_role": e.actor_role,
                    "project_id": _str(e.project_id),
                    "run_id": _str(e.run_id),
                    "action": e.action,
                    "target": e.target,
                    "outcome": e.outcome,
                    "details": e.details,
                }
                for e in rows
            ]


# ------------------------------------------------------------------ helpers --
def _detached(s: Session, project_id: uuid.UUID) -> Project:
    """A project row usable after the session closes (access was checked by the caller)."""
    project = s.get(Project, project_id)
    if project is None:
        raise NotFoundError("project not found")
    s.expunge(project)
    return project


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _str(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _goal(kind: str, params: dict[str, Any]) -> str:
    title = params.get("title")
    return f"{kind}: {title}"[:200] if title else kind


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        c.key: (
            str(v)
            if isinstance(v := getattr(f, c.key), uuid.UUID)
            else _iso(v)
            if hasattr(v, "isoformat")
            else v
        )
        for c in Finding.__table__.columns
    }
