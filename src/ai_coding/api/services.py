"""Server services: authentication, project-scoped RBAC, projects, runs, results.

:class:`Server` is the facade used by the HTTP layer, the CLI and the workers. It is
assembled from one service per area, all built on :class:`~ai_coding.api.core.ServerCore`:

* :mod:`.core` — errors, the caller (:class:`Principal`), sessions, audit records and
  :meth:`~ai_coding.api.core.ServerCore.require`, the project-scoped access check every
  data access goes through (a foreign project is indistinguishable from a missing one,
  AC-08);
* :mod:`.idempotency` — ``Idempotency-Key`` replay;
* :mod:`.accounts` — users, login and API tokens;
* :mod:`.projects` — projects, their configuration and members;
* :mod:`.runs` — runs, approvals, cancellation, artifacts, findings and the audit trail.

Each operation is recorded in the append-only audit trail with actor, project, run and
outcome (ТЗ §3).
"""

from __future__ import annotations

from ai_coding.api.accounts import AccountService
from ai_coding.api.core import (
    ApiError,
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    ServerCore,
    UnauthorizedError,
)
from ai_coding.api.idempotency import IdempotencyService
from ai_coding.api.runs import RunService


class Server(IdempotencyService, AccountService, RunService):
    """Facade used by the HTTP layer, the CLI and the workers."""


__all__ = [
    "ApiError",
    "ConflictError",
    "ForbiddenError",
    "InvalidError",
    "NotFoundError",
    "Principal",
    "Server",
    "ServerCore",
    "UnauthorizedError",
]
