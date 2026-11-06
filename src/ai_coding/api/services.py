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
* :mod:`.runs` — runs, approvals, cancellation, artifacts, findings and the audit trail;
* :mod:`.insights` — read-only views for the Web UI: the agent catalogue, runs and
  findings across the caller's projects, agent executions, LLM usage, system status;
* :mod:`.workspaces` — members' workspaces and project files for the Web UI.

Each operation is recorded in the append-only audit trail with actor, project, run and
outcome (ТЗ §3).
"""

from __future__ import annotations

from ai_coding.api.accounts import AccountService
from ai_coding.api.ai_settings import AIProviderService
from ai_coding.api.core import (
    ApiError,
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    ServerCore,
    TooLargeError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
)
from ai_coding.api.deploys import DeployService
from ai_coding.api.github_service import GitHubService
from ai_coding.api.idempotency import IdempotencyService
from ai_coding.api.insights import InsightService
from ai_coding.api.project_settings import ProjectSettingsService
from ai_coding.api.runs import RunService
from ai_coding.api.test_results import TestResultService
from ai_coding.api.workspaces import WorkspaceService


class Server(
    IdempotencyService,
    InsightService,
    DeployService,
    GitHubService,
    TestResultService,
    AIProviderService,
    ProjectSettingsService,
    AccountService,
    RunService,
    WorkspaceService,
):
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
    "TooLargeError",
    "UnauthorizedError",
    "UnsupportedMediaTypeError",
]
