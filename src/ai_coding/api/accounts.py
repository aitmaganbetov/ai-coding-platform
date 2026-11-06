"""Users, passwords and API tokens (ТЗ §3): argon2id, project-scoped tokens with a
role cap and a lifetime; ``agent`` tokens are short-lived and project-bound."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_coding.api.core import (
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    ServerCore,
    iso_or_none,
    str_or_none,
)
from ai_coding.api.security import (
    hash_password,
    looks_like_token,
    needs_rehash,
    new_token,
    token_hash,
    verify_password,
)
from ai_coding.domain.enums import Outcome, Role
from ai_coding.persistence.models import (
    ApiToken,
    Credential,
    User,
)
from ai_coding.persistence.types import utcnow

_DUMMY_HASH = hash_password("dummy-password-for-timing")
_LAST_USED_GRANULARITY = timedelta(minutes=1)


class AccountService(ServerCore):
    """Users, login and API tokens."""

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
            self.record(
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
            self.record(s, actor, "user.password", Outcome.SUCCESS, target=username)

    # ------------------------------------------------------------- admin --
    def require_admin(self, principal: Principal, action: str) -> None:
        """Instance administrators with an unscoped token only; a refusal is recorded."""
        if principal.is_admin and principal.token_project_id is None:
            return
        self._denied(principal.username, "access.admin", target=action)
        raise ForbiddenError(f"only instance administrators may {action}")

    def list_users(self, principal: Principal) -> list[dict[str, Any]]:
        """All users of the instance (administrators)."""
        self.require_admin(principal, "list users")
        with self.session() as s:
            return [_user_row(u) for u in s.scalars(select(User).order_by(User.username))]

    def update_user(
        self,
        principal: Principal,
        username: str,
        *,
        display_name: str | None = None,
        is_admin: bool | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """Change a user's name, administrator flag or status (administrators).

        Blocking revokes every token of the user: a blocked user is signed out at once
        and, once unblocked, signs in again. An administrator can neither demote nor block
        themselves, so the instance always keeps an active administrator.
        """
        self.require_admin(principal, "change users")
        with self.session() as s:
            user = s.scalar(select(User).where(User.username == username))
            if user is None:
                raise NotFoundError(f"user '{username}' not found")
            if user.id == principal.user_id and (is_admin is False or is_active is False):
                raise ConflictError("administrators cannot demote or block themselves")
            changes: dict[str, Any] = {}
            shown = (display_name.strip() or user.username) if display_name is not None else None
            if shown is not None and shown != user.display_name:
                user.display_name = shown
                changes["display_name"] = shown
            if is_admin is not None and is_admin != user.is_admin:
                user.is_admin = is_admin
                changes["is_admin"] = is_admin
            if is_active is not None and is_active != user.is_active:
                user.is_active = is_active
                changes["is_active"] = is_active
                if not is_active:
                    changes["tokens_revoked"] = _revoke_all_tokens(s, user.id)
            if changes:
                self.record(
                    s,
                    principal.username,
                    "user.update",
                    Outcome.SUCCESS,
                    target=user.username,
                    details=changes,
                )
            s.flush()
            return _user_row(user)

    def reset_password(self, principal: Principal, username: str, password: str) -> None:
        """Set another user's password (administrators)."""
        self.require_admin(principal, "reset passwords")
        self.set_password(username, password, actor=principal.username)

    def change_own_password(self, principal: Principal, current: str, new: str) -> None:
        """Replace the caller's password after checking the current one."""
        if principal.token_project_id is not None:
            raise ForbiddenError("a project-scoped token cannot change the password")
        with self.session() as s:
            cred = s.get(Credential, principal.user_id)
            if cred is None or not verify_password(cred.password_hash, current):
                self._denied(principal.username, "user.password", target=principal.username)
                # Not 401: the caller's token is valid, only the old password is wrong.
                raise InvalidError("the current password is incorrect")
            cred.password_hash = hash_password(new)
            self.record(
                s, principal.username, "user.password", Outcome.SUCCESS, target=principal.username
            )

    def login(self, username: str, password: str) -> Principal | None:
        """Verify a password; ``None`` on failure (same timing for unknown users)."""
        with self.session() as s:
            user = s.scalar(select(User).where(User.username == username))
            cred = s.get(Credential, user.id) if user is not None else None
            ok = verify_password(cred.password_hash if cred else _DUMMY_HASH, password)
            if not (ok and user is not None and cred is not None and user.is_active):
                self.record(s, username[:150] or "-", "auth.login", Outcome.DENIED)
                return None
            if needs_rehash(cred.password_hash):
                cred.password_hash = hash_password(password)
            self.record(s, user.username, "auth.login", Outcome.SUCCESS)
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
            if project_id is not None and self.role_in(s, principal, project_id) is None:
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
            self.record(
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
                    "project_id": str_or_none(r.project_id),
                    "max_role": r.max_role,
                    "created_at": iso_or_none(r.created_at),
                    "expires_at": iso_or_none(r.expires_at),
                    "last_used_at": iso_or_none(r.last_used_at),
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
            self.record(
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


def _user_row(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "created_at": iso_or_none(user.created_at),
    }


def _revoke_all_tokens(s: Session, user_id: uuid.UUID) -> int:
    """Revoke the user's live tokens; their number."""
    now = utcnow()
    live = s.scalars(
        select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
    ).all()
    for row in live:
        row.revoked_at = now
    return len(live)
