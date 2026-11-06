"""Server-side RBAC (ТЗ §3, NFR-04): permissions per project role.

| Role | Rights |
| --- | --- |
| owner | everything in the project, members, export, deletion, audit trail |
| maintainer | manage runs (create, cancel any), approve, audit trail |
| developer | create runs, view results, cancel own runs |
| auditor | read reports, logs and the audit trail; no changes |
| agent | read the project's runs and results with a short-lived, project-scoped token |

Instance administrators manage users. Every user creates projects (Git URL, uploaded
folder, server directory) and becomes their owner, unless
``AI_CODING_SERVER_PROJECT_CREATORS=admins`` reserves that to administrators.
Administrators get no implicit access to a project's data (least privilege): they must
be made members like anyone else.
"""

from __future__ import annotations

from enum import StrEnum

from ai_coding.domain.enums import Role


class Permission(StrEnum):
    """Actions checked by the API before any data access."""

    PROJECT_VIEW = "project.view"
    RUN_VIEW = "run.view"
    RUN_CREATE = "run.create"
    RUN_CREATE_CHANGES = "run.create_changes"  # kinds that commit code (develop, maintain)
    RUN_CANCEL_OWN = "run.cancel_own"
    RUN_CANCEL_ANY = "run.cancel_any"
    RUN_APPROVE = "run.approve"
    FINDINGS_VIEW = "findings.view"
    ARTIFACTS_VIEW = "artifacts.view"
    AUDIT_VIEW = "audit.view"
    FILES_VIEW = "files.view"  # read the project's files in the Web UI
    WORKSPACE_USE = "workspace.use"  # own workspace: terminal, editing, commits
    MEMBERS_MANAGE = "members.manage"
    PROJECT_EXPORT = "project.export"
    PROJECT_DELETE = "project.delete"


_READ = {
    Permission.PROJECT_VIEW,
    Permission.RUN_VIEW,
    Permission.FINDINGS_VIEW,
    Permission.ARTIFACTS_VIEW,
}

MATRIX: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.MAINTAINER: frozenset(
        {
            *_READ,
            Permission.RUN_CREATE,
            Permission.RUN_CREATE_CHANGES,
            Permission.RUN_CANCEL_OWN,
            Permission.RUN_CANCEL_ANY,
            Permission.RUN_APPROVE,
            Permission.AUDIT_VIEW,
            Permission.FILES_VIEW,
            Permission.WORKSPACE_USE,
        }
    ),
    Role.DEVELOPER: frozenset(
        {
            *_READ,
            Permission.RUN_CREATE,
            Permission.RUN_CREATE_CHANGES,
            Permission.RUN_CANCEL_OWN,
            Permission.FILES_VIEW,
            Permission.WORKSPACE_USE,
        }
    ),
    Role.AUDITOR: frozenset({*_READ, Permission.AUDIT_VIEW, Permission.FILES_VIEW}),
    Role.AGENT: frozenset(_READ),
}

# Ordering used to cap a role by a token's ``max_role`` (higher = more rights).
_RANK = {Role.AGENT: 0, Role.AUDITOR: 1, Role.DEVELOPER: 2, Role.MAINTAINER: 3, Role.OWNER: 4}


def allowed(role: Role | None, permission: Permission) -> bool:
    """Whether ``role`` grants ``permission`` (non-members get nothing)."""
    return role is not None and permission in MATRIX[role]


def capped(role: Role | None, cap: Role | None) -> Role | None:
    """The weaker of a member's role and a token's ``max_role``.

    ``auditor`` and ``agent`` are not comparable with change rights; capping a
    developer at ``auditor`` yields ``auditor`` (read-only), never more.
    """
    if role is None or cap is None:
        return role
    return role if _RANK[role] <= _RANK[cap] else cap
