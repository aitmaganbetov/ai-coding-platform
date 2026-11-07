"""HTTP routes: GitHub Actions of the project's repository (ADR-0043).
Registered by :func:`ai_coding.api.app.create_app`.

The token is a project secret (``github.token``, set through the secrets routes) and is
never returned. Input is validated by the service *after* the access checks.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ai_coding.api.app import API, Caller, server_of

router = APIRouter(prefix=API)


class DispatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=255)
    inputs: dict[str, str] = Field(default_factory=dict)


@router.get("/github/status", tags=["github"])
async def github_status(request: Request, caller: Caller) -> dict[str, Any]:
    """The GitHub connection of each project the caller can see (configured, turned on,
    the latest call's outcome) — never a token."""
    return await run_in_threadpool(server_of(request).github_status, caller)


@router.get("/projects/{project_id}/github", tags=["github"])
async def github_settings(
    project_id: uuid.UUID, request: Request, caller: Caller
) -> dict[str, Any]:
    """The repository settings, whether a token is set (its hint for those who manage it)."""
    return await run_in_threadpool(server_of(request).github_settings, caller, project_id)


@router.put("/projects/{project_id}/github", tags=["github"])
async def put_github_settings(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    body: dict[str, Any] = Body(...),  # noqa: B008 - FastAPI's way
) -> dict[str, Any]:
    """``{repository: "owner/name", api_url?, default_ref?, version?}``; ``version`` (the
    one read) refuses to overwrite a change made meanwhile."""
    return await run_in_threadpool(server_of(request).put_github_settings, caller, project_id, body)


@router.get("/projects/{project_id}/github/check", tags=["github"])
async def github_check(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """Whether the repository answers with the saved token: ``ok`` with the repository and
    the token's permissions there, or ``ok: false`` with the reason."""
    return await run_in_threadpool(server_of(request).github_check, caller, project_id)


@router.get("/projects/{project_id}/github/workflows", tags=["github"])
async def github_workflows(
    project_id: uuid.UUID, request: Request, caller: Caller
) -> list[dict[str, Any]]:
    return await run_in_threadpool(server_of(request).github_workflows, caller, project_id)


@router.get("/projects/{project_id}/github/runs", tags=["github"])
async def github_runs(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    *,
    workflow: str | None = None,
    branch: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return await run_in_threadpool(
        lambda: server_of(request).github_runs(
            caller, project_id, workflow=workflow or None, branch=branch or None, limit=limit
        )
    )


@router.get("/projects/{project_id}/github/runs/{gh_run_id}/jobs", tags=["github"])
async def github_run_jobs(
    project_id: uuid.UUID, gh_run_id: str, request: Request, caller: Caller
) -> list[dict[str, Any]]:
    return await run_in_threadpool(
        server_of(request).github_run_jobs, caller, project_id, gh_run_id
    )


@router.post("/projects/{project_id}/github/workflows/{workflow}/dispatch", tags=["github"])
async def github_dispatch(
    project_id: uuid.UUID, workflow: str, body: DispatchIn, request: Request, caller: Caller
) -> dict[str, Any]:
    """Start a ``workflow_dispatch`` workflow on ``ref`` (audited)."""
    return await run_in_threadpool(
        server_of(request).github_dispatch, caller, project_id, workflow, body.ref, body.inputs
    )


__all__ = ["router"]
