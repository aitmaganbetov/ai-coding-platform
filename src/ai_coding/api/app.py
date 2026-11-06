"""FastAPI application: REST API of ТЗ §13 under ``/api/v1`` (ADR-0014, ADR-0030).

Security (NFR-04): bearer API tokens (``Authorization: Bearer aic_…``), server-side
RBAC on every endpoint (:mod:`ai_coding.api.services`), rate limiting per principal
and per client for logins, request size limit, security headers, no CORS by default.
TLS is terminated by the reverse proxy (``deploy/Caddyfile``).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_coding import __version__
from ai_coding.api import lifecycle
from ai_coding.api.agent_bridge import AgentBridge
from ai_coding.api.diffs import PATH_OPS, patch_path, review_payload, validate_patch, validate_path
from ai_coding.api.kinds import KINDS
from ai_coding.api.limits import RateLimiter, SharedRateLimiter, client_address
from ai_coding.api.metrics import Metrics
from ai_coding.api.security import same_digest
from ai_coding.api.services import (
    ApiError,
    ConflictError,
    InvalidError,
    Principal,
    Server,
    TooLargeError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
)
from ai_coding.api.terminal import (
    bridge,
    chat_bridge,
    first_message,
    parse_status,
    refuse,
    terminal_size,
)
from ai_coding.api.workspace_broker import (
    Broker,
    BrokerError,
    GitOutcome,
    WorkspaceRef,
    make_broker,
)
from ai_coding.domain.enums import Role
from ai_coding.observability import get_logger

_log = get_logger(__name__)
API = "/api/v1"
LOGIN_TOKEN_HOURS = 12
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # Inline *styles* are allowed: the browser terminal (xterm.js) sizes and themes itself
    # with <style> elements. Scripts stay 'self' only (ADR-0036). connect-src 'self'
    # covers the terminal's same-origin WebSocket (CSP Level 3).
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
}
_DOCS_CSP = (  # Swagger UI loads its assets from a CDN and runs an inline bootstrap script
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'"
)


# ------------------------------------------------------------------ schemas --
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(_In):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)


class TokenIn(_In):
    name: str = Field(min_length=1, max_length=100)
    project_id: uuid.UUID | None = None
    max_role: Role | None = None
    ttl_hours: int | None = Field(default=None, ge=1, le=24 * 365)


class UserIn(_In):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=12, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool = False


class UserPatchIn(_In):
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool | None = None
    is_active: bool | None = None


class PasswordIn(_In):
    password: str = Field(min_length=12, max_length=1024)


class PasswordChangeIn(_In):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class ProjectIn(_In):
    name: str = Field(min_length=1, max_length=100)
    git_url: str | None = Field(default=None, max_length=2000)
    branch: str | None = Field(default=None, max_length=255)
    path: str | None = Field(default=None, max_length=4096)
    # Opens a private HTTPS repository for the clone only; never stored or logged.
    git_token: str | None = Field(default=None, min_length=1, max_length=1000)


class MemberIn(_In):
    role: Role


class FileIn(_In):
    content: str = Field(max_length=1_000_000)
    # The version the editor loaded: the save is refused if the file changed meanwhile.
    version: str | None = Field(default=None, max_length=64)
    create: bool = False  # a new file only: 409 if it exists


class CommitIn(_In):
    message: str = Field(min_length=1, max_length=2000)
    staged_only: bool = False  # "commit accepted": the index alone (AI Changes Review)


class ReviewIn(_In):
    """A decision on a change of the workspace (ADR-0040): a hunk's patch, or a new file."""

    action: Literal["accept", "reject", "unstage", "accept_file", "reject_file"]
    patch: str | None = Field(default=None, max_length=1024 * 1024)
    path: str | None = Field(default=None, max_length=4096)


class RunIn(_In):
    kind: str = Field(min_length=1, max_length=50)
    params: dict[str, Any] = Field(default_factory=dict)


class DecisionIn(_In):
    approve: bool = True
    approval_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=2000)


class CancelIn(_In):
    reason: str = Field(default="cancelled via API", max_length=500)


# --------------------------------------------------------------- plumbing --
class UnavailableError(ApiError):
    status, code = 503, "unavailable"


class RateLimitedError(Exception):
    """Raised by the limiter dependency; rendered as 429 with ``Retry-After``."""

    def __init__(self, wait: float) -> None:
        super().__init__("rate limited")
        self.wait = wait


def server_of(request: Request) -> Server:
    server: Server = request.app.state.server
    return server


def client_of(request: Request) -> str:
    peer = request.client.host if request.client else None
    trusted = server_of(request).settings.trusted_proxies
    return client_address(peer, request.headers.get("x-forwarded-for"), trusted)


def authenticated(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """The caller of a bearer token (401 otherwise), within its rate limit."""
    scheme, _, token = (authorization or "").partition(" ")
    server = server_of(request)
    found = server.authenticate(token.strip()) if scheme.lower() == "bearer" else None
    if found is None:
        raise UnauthorizedError("missing or invalid API token")
    wait = request.app.state.limiter.check(f"user:{found.user_id}")
    if wait is not None:
        raise RateLimitedError(wait)
    return found


Caller = Annotated[Principal, Depends(authenticated)]
Srv = Annotated[Server, Depends(server_of)]


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


async def idempotent_call(
    request: Request,
    response: Response,
    caller: Principal,
    key: str | None,
    produce: Callable[[], tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    """Run ``produce`` (in a thread) honouring an optional ``Idempotency-Key``."""
    server = server_of(request)
    if key is None:
        status, payload = await run_in_threadpool(produce)
        replayed = False
    else:
        body = await request.body()

        def run() -> tuple[int, dict[str, Any], bool]:
            return server.idempotent(
                caller,
                key,
                method=request.method,
                path=request.url.path,
                body=body,
                produce=produce,
            )

        status, payload, replayed = await run_in_threadpool(run)
    response.status_code = status
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return payload


# -------------------------------------------------------------------- ops --
ops = APIRouter(include_in_schema=False)


@ops.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@ops.get("/readyz")
def readyz(server: Srv) -> JSONResponse:
    try:
        with server.session() as s:
            s.execute(text("SELECT 1"))
    except Exception:
        return error_response(503, "unavailable", "database unavailable")
    return JSONResponse({"status": "ready"})


@ops.get("/metrics")
def prometheus(
    request: Request, server: Srv, authorization: Annotated[str | None, Header()] = None
) -> Response:
    """Metrics for the metrics token (scraper) or an unscoped admin token."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    expected = server.settings.metrics_token
    allowed = expected is not None and same_digest(token, expected)
    if not allowed:
        found = server.authenticate(token) if scheme.lower() == "bearer" else None
        allowed = found is not None and found.is_admin and found.token_project_id is None
    if not allowed:
        raise UnauthorizedError("metrics need the metrics token or an admin token")
    metrics: Metrics = request.app.state.metrics
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


# ------------------------------------------------------------------- auth --
auth = APIRouter(prefix=API, tags=["auth"])


@auth.post("/auth/login", status_code=201)
def login(body: LoginIn, request: Request, server: Srv) -> dict[str, Any]:
    """Exchange a password for an API token valid for 12 hours."""
    wait = request.app.state.login_limiter.check(f"login:{client_of(request)}")
    if wait is not None:
        raise RateLimitedError(wait)
    found = server.login(body.username, body.password)
    if found is None:
        raise UnauthorizedError("invalid username or password")
    token, token_id = server.issue_token(found, "login", ttl_hours=LOGIN_TOKEN_HOURS)
    return {
        "token": token,
        "token_id": str(token_id),
        "username": found.username,
        "is_admin": found.is_admin,
        "expires_in_hours": LOGIN_TOKEN_HOURS,
    }


@auth.get("/me")
def me(caller: Caller, server: Srv) -> dict[str, Any]:
    scope = caller.token_project_id
    return {
        "username": caller.username,
        "is_admin": caller.is_admin,
        "can_create_projects": server.can_create_projects(caller),
        "token_id": str(caller.token_id) if caller.token_id else None,
        "token_project_id": str(scope) if scope else None,
        "token_max_role": caller.token_max_role.value if caller.token_max_role else None,
    }


@auth.put("/me/password", status_code=204)
def change_password(body: PasswordChangeIn, caller: Caller, server: Srv) -> Response:
    """Replace the caller's password; the current one is required."""
    server.change_own_password(caller, body.current_password, body.new_password)
    return Response(status_code=204)


@auth.get("/tokens")
def tokens(caller: Caller, server: Srv) -> list[dict[str, Any]]:
    return server.list_tokens(caller)


@auth.post("/tokens", status_code=201)
def issue_token(body: TokenIn, caller: Caller, server: Srv) -> dict[str, Any]:
    """New API token; the token is shown only in this response."""
    token, token_id = server.issue_token(
        caller,
        body.name,
        project_id=body.project_id,
        max_role=body.max_role,
        ttl_hours=body.ttl_hours,
    )
    return {"token": token, "token_id": str(token_id)}


@auth.post("/tokens/{token_id}/rotate", status_code=201)
def rotate_token(token_id: uuid.UUID, caller: Caller, server: Srv) -> dict[str, Any]:
    token, new_id = server.rotate_token(caller, token_id)
    return {"token": token, "token_id": str(new_id)}


@auth.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: uuid.UUID, caller: Caller, server: Srv) -> Response:
    server.revoke_token(caller, token_id)
    return Response(status_code=204)


@auth.get("/users", tags=["admin"])
def list_users(caller: Caller, server: Srv) -> list[dict[str, Any]]:
    return server.list_users(caller)


@auth.post("/users", status_code=201, tags=["admin"])
def create_user(body: UserIn, caller: Caller, server: Srv) -> dict[str, Any]:
    server.require_admin(caller, "create users")
    user_id = server.create_user(
        body.username,
        body.password,
        display_name=body.display_name,
        is_admin=body.is_admin,
        actor=caller.username,
    )
    return {"id": str(user_id), "username": body.username}


@auth.patch("/users/{username}", tags=["admin"])
def update_user(username: str, body: UserPatchIn, caller: Caller, server: Srv) -> dict[str, Any]:
    """Rename, grant or withdraw administrator rights, block (revokes all tokens) or
    unblock a user."""
    return server.update_user(
        caller,
        username,
        display_name=body.display_name,
        is_admin=body.is_admin,
        is_active=body.is_active,
    )


@auth.put("/users/{username}/password", status_code=204, tags=["admin"])
def reset_password(username: str, body: PasswordIn, caller: Caller, server: Srv) -> Response:
    server.reset_password(caller, username, body.password)
    return Response(status_code=204)


# --------------------------------------------------------------- projects --
projects = APIRouter(prefix=API, tags=["projects"])


@projects.get("/projects")
def list_projects(caller: Caller, server: Srv) -> list[dict[str, Any]]:
    return server.list_projects(caller)


@projects.get("/project-sources", tags=["admin"])
def project_sources(caller: Caller, server: Srv) -> dict[str, Any]:
    """Where a project can be registered from: Git, and the directories under
    ``AI_CODING_SERVER_ALLOWED_ROOTS`` (instance admins)."""
    return server.project_sources(caller)


@projects.post("/projects", status_code=201)
async def create_project(
    request: Request,
    response: Response,
    body: ProjectIn,
    caller: Caller,
    *,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Register a project from a Git URL or a server directory; the caller becomes its
    owner. Idempotent with ``Idempotency-Key``."""
    server = server_of(request)

    def produce() -> tuple[int, dict[str, Any]]:
        pid = server.create_project(
            caller,
            body.name,
            git_url=body.git_url,
            branch=body.branch,
            path=body.path,
            git_token=body.git_token,
        )
        return 201, server.get_project(caller, pid)

    return await idempotent_call(request, response, caller, idempotency_key, produce)


UPLOAD_PATH = f"{API}/projects/upload"
_TAR_TYPES = {"application/x-tar", "application/tar", "application/octet-stream"}


@projects.post("/projects/upload", status_code=201)
async def upload_project(
    request: Request,
    caller: Caller,
    name: Annotated[str, Query(min_length=1, max_length=100)],
    content_type: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Register a project from a folder uploaded from the caller's computer.

    The body is an **uncompressed tar** of the folder (``Content-Type:
    application/x-tar``), at most ``AI_CODING_SERVER_MAX_UPLOAD_MB``. The files become
    the first commit of a new Git repository; the caller becomes the owner.
    """
    server = server_of(request)
    if (content_type or "").split(";")[0].strip().lower() not in _TAR_TYPES:
        raise UnsupportedMediaTypeError("send the folder as an uncompressed tar archive")
    await run_in_threadpool(server.check_new_project, caller, name)  # before the body
    limit = server.settings.max_upload_bytes
    uploads = server.settings.data_dir / "uploads"
    await run_in_threadpool(uploads.mkdir, parents=True, exist_ok=True)
    fd, name_on_disk = tempfile.mkstemp(suffix=".tar", dir=uploads)
    archive = Path(name_on_disk)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:  # also without Content-Length (chunked)
                    raise TooLargeError(f"the folder is larger than {limit / 2**20:g} MB")
                await run_in_threadpool(out.write, chunk)
        pid = await run_in_threadpool(server.create_uploaded_project, caller, name, archive)
        return await run_in_threadpool(server.get_project, caller, pid)
    finally:
        archive.unlink(missing_ok=True)


@projects.get("/projects/{project_id}")
def get_project(project_id: uuid.UUID, caller: Caller, server: Srv) -> dict[str, Any]:
    return server.get_project(caller, project_id)


@projects.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, caller: Caller, server: Srv) -> Response:
    """Delete the project and its data (owner, NFR-08); the audit trail is kept."""
    lifecycle.delete_project(server, caller, project_id)
    return Response(status_code=204)


@projects.get("/projects/{project_id}/export")
def export_project(project_id: uuid.UUID, caller: Caller, server: Srv) -> Response:
    """ZIP export of everything the platform stores about the project (owner, NFR-08)."""
    content = lifecycle.export_project(server, caller, project_id)
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="project-{project_id}.zip"'},
    )


@projects.put("/projects/{project_id}/members/{username}")
def put_member(
    project_id: uuid.UUID, username: str, body: MemberIn, caller: Caller, server: Srv
) -> dict[str, Any]:
    server.set_member(caller, project_id, username, body.role)
    return server.get_project(caller, project_id)


@projects.delete("/projects/{project_id}/members/{username}", status_code=204)
def delete_member(project_id: uuid.UUID, username: str, caller: Caller, server: Srv) -> Response:
    server.set_member(caller, project_id, username, None)
    return Response(status_code=204)


@projects.get("/projects/{project_id}/findings")
def findings(
    project_id: uuid.UUID,
    caller: Caller,
    server: Srv,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[dict[str, Any]]:
    return server.findings(caller, project_id, limit=limit)


@projects.get("/audit-events", tags=["audit"])
def audit_events(
    caller: Caller,
    server: Srv,
    project_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[dict[str, Any]]:
    return server.audit_events(caller, project_id, limit=limit)


# -------------------------------------------------------------------- runs --
runs = APIRouter(prefix=API, tags=["runs"])


@runs.get("/kinds")
def kinds(_caller: Caller) -> list[dict[str, Any]]:
    return [
        {
            "name": k.name,
            "mode": k.mode.value,
            "permission": k.permission.value,
            "description": k.description,
            "params": k.params.model_json_schema(),
        }
        for k in KINDS.values()
    ]


@runs.get("/projects/{project_id}/runs")
def list_runs(
    project_id: uuid.UUID,
    caller: Caller,
    server: Srv,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    return server.list_runs(caller, project_id, limit=limit)


@runs.post("/projects/{project_id}/runs", status_code=201)
async def create_run(
    project_id: uuid.UUID,
    request: Request,
    response: Response,
    body: RunIn,
    caller: Caller,
    *,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Plan a run and queue it for the workers; idempotent with ``Idempotency-Key``."""
    server = server_of(request)

    def produce() -> tuple[int, dict[str, Any]]:
        run_id = server.create_run(
            caller, project_id, body.kind, body.params, idempotency_key=idempotency_key
        )
        return 201, server.run_status(caller, run_id)

    return await idempotent_call(request, response, caller, idempotency_key, produce)


@runs.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, caller: Caller, server: Srv) -> dict[str, Any]:
    return server.run_status(caller, run_id)


@runs.post("/runs/{run_id}/approve")
def approve(run_id: uuid.UUID, body: DecisionIn, caller: Caller, server: Srv) -> dict[str, Any]:
    """Approve (or, with ``approve: false``, reject) pending approvals of the run."""
    return server.decide(
        caller, run_id, approve=body.approve, approval_id=body.approval_id, note=body.note
    )


@runs.post("/runs/{run_id}/cancel")
def cancel(
    run_id: uuid.UUID, caller: Caller, server: Srv, body: CancelIn | None = None
) -> dict[str, Any]:
    return server.cancel(caller, run_id, (body or CancelIn()).reason)


@runs.get("/runs/{run_id}/artifacts")
def list_artifacts(run_id: uuid.UUID, caller: Caller, server: Srv) -> list[dict[str, Any]]:
    return server.artifacts(caller, run_id)


@runs.get("/runs/{run_id}/artifacts/{artifact_id}")
def download_artifact(
    run_id: uuid.UUID, artifact_id: uuid.UUID, caller: Caller, server: Srv
) -> Response:
    name, media, content = server.artifact_content(caller, run_id, artifact_id)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120] or "artifact"
    return Response(
        content,
        media_type=media or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


# ---------------------------------------------------------------- insights --
insights = APIRouter(prefix=API)
Filter = Annotated[str | None, Query(max_length=200)]  # empty or unknown: matches nothing


@insights.get("/agents", tags=["agents"])
def agents(_caller: Caller, server: Srv) -> list[dict[str, Any]]:
    """The agents of ТЗ §6: inputs, outputs, LLM, capabilities, allowed tools."""
    return server.agents()


@insights.get("/runs", tags=["runs"])
def all_runs(
    caller: Caller, server: Srv, *, limit: Annotated[int, Query(ge=1)] = 50,
    state: Filter = None, kind: Filter = None, project_id: Filter = None,
) -> list[dict[str, Any]]:  # fmt: skip
    """Runs of every project the caller may view, newest first (at most 200)."""
    return server.all_runs(caller, limit=limit, state=state, kind=kind, project_id=project_id)


@insights.get("/findings", tags=["projects"])
def all_findings(
    caller: Caller, server: Srv, *, limit: Annotated[int, Query(ge=1)] = 500,
    severity: Filter = None, status: Filter = None, source: Filter = None,
    project_id: Filter = None, run_id: Filter = None,
) -> list[dict[str, Any]]:  # fmt: skip
    """Findings of every project the caller may view, newest first (at most 2000)."""
    return server.all_findings(caller, limit=limit, severity=severity, status=status,
                               source=source, project_id=project_id, run_id=run_id)  # fmt: skip


@insights.get("/projects/{project_id}/agent-executions", tags=["runs"])
def agent_executions(
    project_id: uuid.UUID, caller: Caller, server: Srv, limit: Annotated[int, Query(ge=1)] = 100
) -> list[dict[str, Any]]:
    """Attempts of the project's tasks with their LLM tokens, cost and models (at most 500)."""
    return server.agent_executions(caller, project_id, limit=limit)


@insights.get("/projects/{project_id}/usage", tags=["runs"])
def usage(
    project_id: uuid.UUID,
    caller: Caller,
    server: Srv,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """LLM tokens and cost of the project's runs: totals, per model, per UTC day."""
    return server.usage(caller, project_id, days=days)


@insights.get("/system", tags=["admin"])
def system(request: Request, caller: Caller, server: Srv) -> dict[str, Any]:
    """Version, database, queue, runs, workers and analysers' offline data (administrators)."""
    return server.system_status(caller, workspaces=request.app.state.broker is not None)


# --------------------------------------------------------------- workspace --
workspace = APIRouter(prefix=API, tags=["workspace"])
Scope = Annotated[Literal["workspace", "project"], Query()]
FilePath = Annotated[str, Query(max_length=4096)]


def broker_of(request: Request) -> Broker:
    broker: Broker | None = request.app.state.broker
    if broker is None:
        raise UnavailableError("workspace terminals are disabled (AI_CODING_SERVER_WORKSPACES)")
    return broker


@workspace.get("/projects/{project_id}/workspace")
def workspace_info(project_id: uuid.UUID, caller: Caller, server: Srv) -> dict[str, Any]:
    """Whether the caller has a workspace in the project and may edit."""
    return server.workspace_info(caller, project_id)


@workspace.post("/projects/{project_id}/workspace", status_code=201)
def open_workspace(project_id: uuid.UUID, caller: Caller, server: Srv) -> dict[str, Any]:
    """The caller's own clone of the project, for the editor and the terminal."""
    return server.create_workspace(caller, project_id)


@workspace.get("/projects/{project_id}/files")
def list_files(
    project_id: uuid.UUID, caller: Caller, server: Srv, path: FilePath = "",
    scope: Scope = "workspace",
) -> list[dict[str, Any]]:  # fmt: skip
    """A directory of the caller's workspace, or (read-only) of the project."""
    return server.list_files(caller, project_id, path, scope)


@workspace.get("/projects/{project_id}/file")
def read_file(
    project_id: uuid.UUID, caller: Caller, server: Srv, path: FilePath = "",
    scope: Scope = "workspace",
) -> dict[str, Any]:  # fmt: skip
    return server.read_file(caller, project_id, path, scope)


@workspace.put("/projects/{project_id}/file")
def write_file(
    project_id: uuid.UUID, body: FileIn, caller: Caller, server: Srv, path: FilePath = ""
) -> dict[str, Any]:
    """Save a file of the caller's workspace (409 if it changed since it was loaded)."""
    return server.write_file(caller, project_id, path, body.content, body.version,
                             create=body.create)  # fmt: skip


@workspace.delete("/projects/{project_id}/file", status_code=204)
def delete_file(
    project_id: uuid.UUID, caller: Caller, server: Srv, path: FilePath = ""
) -> Response:
    server.delete_file(caller, project_id, path)
    return Response(status_code=204)


async def _workspace_git(
    request: Request,
    caller: Principal,
    project_id: uuid.UUID,
    op: str,
    message: str | None = None,
    **extra: str | None,
) -> tuple[WorkspaceRef, GitOutcome]:
    server = server_of(request)
    ref = await run_in_threadpool(server.workspace_ref, caller, project_id)
    return ref, await broker_of(request).git(ref, op, message, **extra)


@workspace.post("/projects/{project_id}/workspace/restart")
async def restart_workspace(
    project_id: uuid.UUID, request: Request, caller: Caller
) -> dict[str, str]:
    """Stop the caller's workspace container (a new one starts on the next use, from the
    current image). Running agents and terminals end; files and sign-ins are kept."""
    server = server_of(request)
    ref = await run_in_threadpool(server.workspace_ref, caller, project_id)
    await broker_of(request).stop(ref)
    await run_in_threadpool(server.record_terminal, caller, project_id, "workspace.restart")
    return {"status": "restarted"}


@workspace.get("/projects/{project_id}/workspace/git")
async def git_status(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """Branch and changed files of the caller's workspace."""
    _, outcome = await _workspace_git(request, caller, project_id, "status")
    if not outcome.ok:
        raise InvalidError(outcome.output.strip() or "git status failed")
    return parse_status(outcome.output)


@workspace.post("/projects/{project_id}/workspace/commit")
async def git_commit(
    project_id: uuid.UUID, body: CommitIn, request: Request, caller: Caller
) -> dict[str, Any]:
    """Commit every change of the workspace on its current branch (``staged_only``: the
    changes accepted in the review alone)."""
    op = "commit_staged" if body.staged_only else "commit"
    _, outcome = await _workspace_git(request, caller, project_id, op, body.message)
    return {"ok": outcome.ok, "output": outcome.output, "branch": outcome.branch}


@workspace.get("/projects/{project_id}/workspace/diff")
async def workspace_diff(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """The workspace's changes for review: accepted (index) and pending (working tree and
    new files), parsed into files and hunks (ADR-0040)."""
    _, outcome = await _workspace_git(request, caller, project_id, "diff")
    if not outcome.ok:
        raise InvalidError(outcome.output.strip() or "git diff failed")
    texts = json.loads(outcome.output)
    return review_payload(texts["staged"], texts["unstaged"], texts["untracked"])


@workspace.post("/projects/{project_id}/workspace/review")
async def workspace_review(
    project_id: uuid.UUID, body: ReviewIn, request: Request, caller: Caller
) -> dict[str, Any]:
    """Accept (stage), reject (revert) or send back (unstage) a hunk; accept or reject a
    new file. A hunk that no longer applies (the file changed meanwhile) is refused."""
    server = server_of(request)
    # Access first (another project's id gets 403/404, never a validation message).
    ref = await run_in_threadpool(server.workspace_ref, caller, project_id)
    try:
        target = validate_path(body.path) if body.action in PATH_OPS else validate_patch(body.patch)
    except ValueError as exc:
        raise InvalidError(str(exc)) from exc
    outcome = await broker_of(request).git(ref, body.action, patch=body.patch, path=body.path)
    if not outcome.ok:
        raise ConflictError(outcome.output.strip()[-1000:] or "the change no longer applies")
    path = target if body.action in PATH_OPS else patch_path(target)
    await run_in_threadpool(server.record_review, caller, project_id, body.action, path)
    return {"ok": True, "output": outcome.output}


@workspace.post("/projects/{project_id}/workspace/pull")
async def git_pull(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """Fetch the project repository and fast-forward the workspace branch."""
    _, outcome = await _workspace_git(request, caller, project_id, "pull")
    return {"ok": outcome.ok, "output": outcome.output, "branch": outcome.branch}


@workspace.post("/projects/{project_id}/workspace/publish")
async def git_publish(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """Copy the workspace branch into the project repository as ``ws/<user>/<branch>``."""
    _, outcome = await _workspace_git(request, caller, project_id, "bundle")
    if outcome.bundle is None or outcome.branch is None:
        raise InvalidError(outcome.output.strip() or "nothing to publish")
    server = server_of(request)
    return await run_in_threadpool(server.publish_bundle, caller, project_id, outcome.bundle,
                                   outcome.branch)  # fmt: skip


@workspace.websocket("/projects/{project_id}/workspace/terminal")
async def workspace_terminal(websocket: WebSocket, project_id: uuid.UUID) -> None:
    """The caller's terminal in their workspace (protocol: :mod:`ai_coding.api.terminal`)."""
    await _workspace_socket(websocket, project_id, chat=False)


@workspace.websocket("/projects/{project_id}/workspace/chat")
async def workspace_chat(websocket: WebSocket, project_id: uuid.UUID) -> None:
    """The caller's AI chat (Claude Code, Codex) running in their workspace container."""
    await _workspace_socket(websocket, project_id, chat=True)


async def _workspace_socket(websocket: WebSocket, project_id: uuid.UUID, *, chat: bool) -> None:
    await websocket.accept()
    server: Server = websocket.app.state.server
    try:
        first = await first_message(websocket)
        found, ref = await _workspace_caller(websocket, server, project_id, first)
        broker: Broker = websocket.app.state.broker
        session = await (broker.chat(ref) if chat else broker.attach(ref, *terminal_size(first)))
    except (ApiError, BrokerError, ValueError, TypeError, TimeoutError) as exc:
        await refuse(websocket, getattr(exc, "message", None) or str(exc) or "refused")
        return
    action = "workspace.chat.open" if chat else "workspace.terminal.open"
    await run_in_threadpool(server.record_terminal, found, project_id, action)
    await (chat_bridge(websocket, session) if chat else bridge(websocket, session))


async def _workspace_caller(
    websocket: WebSocket, server: Server, project_id: uuid.UUID, first: dict[str, Any]
) -> tuple[Principal, WorkspaceRef]:
    """Authenticate the first message and check the caller's role in the project."""
    found = await run_in_threadpool(server.authenticate, str(first.get("token", "")))
    if found is None:
        raise UnauthorizedError("missing or invalid API token")
    if websocket.app.state.limiter.check(f"user:{found.user_id}") is not None:
        raise ApiError("too many requests")
    if websocket.app.state.broker is None:
        raise UnavailableError("workspace terminals are disabled")
    return found, await run_in_threadpool(server.workspace_ref, found, project_id)


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "too_large",
    415: "unsupported_media_type",
}


# --------------------------------------------------------------------- app --
def shared_rate_limits(backend: str, *, is_sqlite: bool, multi_process: bool) -> bool:
    """Whether the API counts rate limits in the database (one budget for all processes).

    ``auto`` shares them on PostgreSQL and whenever several API processes serve the same
    database (``serve --workers N``, SQLite included): an in-process limiter would give
    each process its own budget, N times the limit and the login brute-force budget.
    """
    if backend == "database":
        return True
    if backend == "memory":
        if multi_process:
            _log.warning("rate limits are per process with several API processes "
                         "(AI_CODING_SERVER_RATE_LIMIT_BACKEND=memory): each process "
                         "allows the full limit")  # fmt: skip
        return False
    return multi_process or not is_sqlite


def create_app(
    server: Server,
    *,
    ui_dir: Path | None = None,
    run_worker: bool = False,
    multi_process: bool = False,
    broker: Broker | Literal["auto"] | None = "auto",
    security_center_enabled: bool = False,
) -> FastAPI:
    """The API application bound to ``server`` (and the Web UI, when built).

    ``run_worker`` runs a worker pool inside the API process (small installations);
    larger ones run ``ai-coding server worker`` processes separately. ``multi_process``:
    this is one of several API processes on the same database (``serve --workers N``).
    ``broker`` drives the workspace terminals (default: from ``AI_CODING_SERVER_WORKSPACES``).
    """
    settings = server.settings
    metrics = Metrics(server)
    terminals = make_broker(settings) if broker == "auto" else broker

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks = []
        if terminals is not None:  # removes idle workspace containers
            tasks.append(asyncio.create_task(terminals.run(stop)))
            # The orchestrator as tools of the chat agents (ADR-0038).
            tasks.append(asyncio.create_task(AgentBridge(server).run(stop)))
        if run_worker:
            from ai_coding.api.worker import Worker  # noqa: PLC0415 - only when embedded

            tasks.append(asyncio.create_task(Worker(server, metrics=metrics).run(stop)))
        try:
            yield
        finally:
            stop.set()
            for task in tasks:
                await task

    app = FastAPI(
        lifespan=lifespan,
        title="AI Coding Platform API",
        version=__version__,
        openapi_url=f"{API}/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.server = server
    app.state.broker = terminals
    shared = shared_rate_limits(
        settings.rate_limit_backend, is_sqlite=server.db.is_sqlite, multi_process=multi_process
    )
    if shared:  # one budget for all API processes and replicas
        app.state.limiter = SharedRateLimiter(server.db, settings.rate_limit_per_minute)
        app.state.login_limiter = SharedRateLimiter(server.db, settings.login_rate_limit_per_minute)
    else:
        app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
        app.state.login_limiter = RateLimiter(settings.login_rate_limit_per_minute)
    app.state.metrics = metrics
    _install_handlers(app)
    app.middleware("http")(_guard)
    for router in (ops, auth, projects, runs, insights, workspace):
        app.include_router(router)
    if security_center_enabled:
        center = APIRouter(prefix=API, tags=["security-center"])

        @center.get("/projects/{project_id}/security-center/runs/{run_id}")
        def security_center(
            project_id: uuid.UUID,
            run_id: uuid.UUID,
            caller: Caller,
            server: Srv,
            *,
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
            offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        ) -> dict[str, Any]:
            return server.security_center(caller, project_id, run_id, limit=limit, offset=offset)

        app.include_router(center)
    if ui_dir is not None and (ui_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    return app


async def _guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Size limit, security headers, HSTS behind TLS, request metrics."""
    settings = server_of(request).settings
    started = time.perf_counter()
    length = request.headers.get("content-length")
    upload = request.method == "POST" and request.url.path == UPLOAD_PATH
    limit = settings.max_upload_bytes if upload else settings.max_request_bytes
    if length and length.isdigit() and int(length) > limit:
        response: Response = error_response(413, "too_large", "request body too large")
    else:
        response = await call_next(request)
    headers = dict(SECURITY_HEADERS)
    if request.url.path == "/api/docs":
        headers["Content-Security-Policy"] = _DOCS_CSP
    for name, value in headers.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    peer = request.client.host if request.client else None
    behind_tls = request.headers.get("x-forwarded-proto") == "https" and (
        peer in settings.trusted_proxies
    )
    if request.url.scheme == "https" or behind_tls:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    route = request.scope.get("route")
    request.app.state.metrics.observe_request(
        request.method,
        getattr(route, "path", "unmatched"),
        response.status_code,
        time.perf_counter() - started,
    )
    return response


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        response = error_response(exc.status, exc.code, exc.message)
        if exc.status == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(BrokerError)
    async def workspace_failed(_request: Request, exc: BrokerError) -> JSONResponse:
        return error_response(503, "unavailable", str(exc))

    @app.exception_handler(RateLimitedError)
    async def limited(_request: Request, exc: RateLimitedError) -> JSONResponse:
        response = error_response(429, "rate_limited", "too many requests")
        response.headers["Retry-After"] = str(max(1, int(exc.wait + 0.999)))
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Errors raised by the framework itself (malformed JSON body, unknown route,
        # wrong method) use the same envelope as the API's own errors (found by B-801).
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        response = error_response(exc.status_code, code, message)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "invalid", "message": "invalid request", "details": details}
            },
        )


UI_DIR_ENV = "AI_CODING_SERVER_UI_DIR"


def app_from_env() -> FastAPI:
    """Application factory for ``serve --workers N``: each API process builds its own
    server from the environment (uvicorn imports it by name in every process)."""
    import os  # noqa: PLC0415

    from ai_coding.api.settings import ServerSettings  # noqa: PLC0415
    from ai_coding.observability import configure_logging  # noqa: PLC0415

    configure_logging(json=True, level="INFO")
    ui = os.environ.get(UI_DIR_ENV)
    return create_app(Server(ServerSettings.from_env()), ui_dir=Path(ui) if ui else None,
                      multi_process=True)  # fmt: skip
