"""FastAPI application: REST API of ТЗ §13 under ``/api/v1`` (ADR-0014, ADR-0030).

Security (NFR-04): bearer API tokens (``Authorization: Bearer aic_…``), server-side
RBAC on every endpoint (:mod:`ai_coding.api.services`), rate limiting per principal
and per client for logins, request size limit, security headers, no CORS by default.
TLS is terminated by the reverse proxy (``deploy/Caddyfile``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from ai_coding import __version__
from ai_coding.api import lifecycle
from ai_coding.api.kinds import KINDS
from ai_coding.api.limits import RateLimiter, client_address
from ai_coding.api.metrics import Metrics
from ai_coding.api.security import same_digest
from ai_coding.api.services import (
    ApiError,
    ForbiddenError,
    Principal,
    Server,
    UnauthorizedError,
)
from ai_coding.domain.enums import Role

API = "/api/v1"
LOGIN_TOKEN_HOURS = 12
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
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


class ProjectIn(_In):
    name: str = Field(min_length=1, max_length=100)
    git_url: str | None = Field(default=None, max_length=2000)
    branch: str | None = Field(default=None, max_length=255)
    path: str | None = Field(default=None, max_length=4096)


class MemberIn(_In):
    role: Role


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
def me(caller: Caller) -> dict[str, Any]:
    scope = caller.token_project_id
    return {
        "username": caller.username,
        "is_admin": caller.is_admin,
        "token_project_id": str(scope) if scope else None,
        "token_max_role": caller.token_max_role.value if caller.token_max_role else None,
    }


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


@auth.post("/users", status_code=201, tags=["admin"])
def create_user(body: UserIn, caller: Caller, server: Srv) -> dict[str, Any]:
    if not caller.is_admin or caller.token_project_id is not None:
        raise ForbiddenError("only instance administrators may create users")
    user_id = server.create_user(
        body.username,
        body.password,
        display_name=body.display_name,
        is_admin=body.is_admin,
        actor=caller.username,
    )
    return {"id": str(user_id), "username": body.username}


# --------------------------------------------------------------- projects --
projects = APIRouter(prefix=API, tags=["projects"])


@projects.get("/projects")
def list_projects(caller: Caller, server: Srv) -> list[dict[str, Any]]:
    return server.list_projects(caller)


@projects.post("/projects", status_code=201)
async def create_project(
    request: Request,
    response: Response,
    body: ProjectIn,
    caller: Caller,
    *,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Register a project (instance admins); idempotent with ``Idempotency-Key``."""
    server = server_of(request)

    def produce() -> tuple[int, dict[str, Any]]:
        pid = server.create_project(
            caller, body.name, git_url=body.git_url, branch=body.branch, path=body.path
        )
        return 201, server.get_project(caller, pid)

    return await idempotent_call(request, response, caller, idempotency_key, produce)


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


# --------------------------------------------------------------------- app --
def create_app(server: Server, *, ui_dir: Path | None = None, run_worker: bool = False) -> FastAPI:
    """The API application bound to ``server`` (and the Web UI, when built).

    ``run_worker`` runs a worker pool inside the API process (small installations);
    larger ones run ``ai-coding server worker`` processes separately.
    """
    settings = server.settings
    metrics = Metrics(server)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not run_worker:
            yield
            return
        from ai_coding.api.worker import Worker  # noqa: PLC0415 - only when embedded

        stop = asyncio.Event()
        task = asyncio.create_task(Worker(server, metrics=metrics).run(stop))
        try:
            yield
        finally:
            stop.set()
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
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.login_limiter = RateLimiter(settings.login_rate_limit_per_minute)
    app.state.metrics = metrics
    _install_handlers(app)
    app.middleware("http")(_guard)
    for router in (ops, auth, projects, runs):
        app.include_router(router)
    if ui_dir is not None and (ui_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    return app


async def _guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Size limit, security headers, HSTS behind TLS, request metrics."""
    settings = server_of(request).settings
    started = time.perf_counter()
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_request_bytes:
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

    @app.exception_handler(RateLimitedError)
    async def limited(_request: Request, exc: RateLimitedError) -> JSONResponse:
        response = error_response(429, "rate_limited", "too many requests")
        response.headers["Retry-After"] = str(max(1, int(exc.wait + 0.999)))
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
    return create_app(Server(ServerSettings.from_env()), ui_dir=Path(ui) if ui else None)
