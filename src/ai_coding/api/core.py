"""Core of the server services (ADR-0030): errors, the authenticated caller, database
sessions, audit records and the project-scoped access check every service goes through.

Every data access goes through :meth:`ServerCore.require`, which resolves the caller's
role in the project (capped by the token's scope) and raises :class:`NotFoundError` when
the caller is not a member — a resource of another project is indistinguishable from a
missing one, so nothing leaks across projects (AC-08)."""

from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ai_coding.api.rbac import Permission, allowed, capped
from ai_coding.api.settings import ServerSettings
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import Outcome, Role
from ai_coding.orchestrator.audit import AuditLogger
from ai_coding.persistence import Database, upgrade_to_head
from ai_coding.persistence.models import (
    Project,
    ProjectMember,
    Run,
)


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


class ServerCore:
    """State and access checks shared by the server services."""

    def __init__(self, settings: ServerSettings, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db or Database(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        self.audit = AuditLogger()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._config_cache: dict[tuple[str, int, int], AICodingConfig] = {}
        self._config_lock = threading.Lock()

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

    def record(self, s: Session, actor: str, action: str, outcome: Outcome, **kw: Any) -> None:
        """Audit event in the caller's transaction."""
        self.audit.record(s, actor=actor, action=action, outcome=outcome, **kw)

    def _denied(self, actor: str, action: str, **kw: Any) -> None:
        """Record a refusal in its own transaction: the request's transaction is rolled
        back by the error that follows, but the denial must stay in the audit trail."""
        with self.session() as s:
            self.audit.record(s, actor=actor, action=action, outcome=Outcome.DENIED, **kw)

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
        without the permission → :class:`ForbiddenError`. Both are recorded as denied;
        a probe of a foreign project is a server-level event (no ``project_id``), so it
        never appears in that project's trail, only in the administrators' view.
        """
        role = self._role(s, principal, project_id)
        if role is None:
            self._denied(
                principal.username, f"access.{permission.value}", target=f"project:{project_id}"
            )
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


# ------------------------------------------------------------------ helpers --
def detached_project(s: Session, project_id: uuid.UUID) -> Project:
    """A project row usable after the session closes (access was checked by the caller)."""
    project = s.get(Project, project_id)
    if project is None:
        raise NotFoundError("project not found")
    s.expunge(project)
    return project


def iso_or_none(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def str_or_none(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None
