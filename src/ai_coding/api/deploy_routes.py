"""HTTP routes: deployment environments and deployments (ADR-0048).
Registered by :func:`ai_coding.api.app.create_app`.

Bodies and ids are plain JSON and strings here, validated by the service *after* the
access checks: a caller without access learns nothing, not even whether the input was
valid (tests/security/test_api_access.py).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ai_coding.api.app import API, Caller, server_of

router = APIRouter(prefix=API)


class DeploymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment_id: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=1, max_length=255)


@router.get("/projects/{project_id}/environments", tags=["deployments"])
async def list_environments(
    project_id: uuid.UUID, request: Request, caller: Caller
) -> dict[str, Any]:
    """The environments with their latest deployment; ``can_manage``/``can_deploy`` say
    what the caller may do."""
    return await run_in_threadpool(server_of(request).environments, caller, project_id)


@router.post("/projects/{project_id}/environments", status_code=201, tags=["deployments"])
async def create_environment(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    body: dict[str, Any] = Body(...),  # noqa: B008 - FastAPI's way
) -> dict[str, Any]:
    return await run_in_threadpool(server_of(request).create_environment, caller, project_id, body)


@router.put("/projects/{project_id}/environments/{environment_id}", tags=["deployments"])
async def update_environment(
    project_id: uuid.UUID,
    environment_id: str,
    request: Request,
    caller: Caller,
    body: dict[str, Any] = Body(...),  # noqa: B008 - FastAPI's way
) -> dict[str, Any]:
    return await run_in_threadpool(
        server_of(request).update_environment, caller, project_id, environment_id, body
    )


@router.delete(
    "/projects/{project_id}/environments/{environment_id}", status_code=204, tags=["deployments"]
)
async def delete_environment(
    project_id: uuid.UUID, environment_id: str, request: Request, caller: Caller
) -> Response:
    """Remove an environment, its history and its webhook secret (not while deploying)."""
    await run_in_threadpool(
        server_of(request).delete_environment, caller, project_id, environment_id
    )
    return Response(status_code=204)


@router.get("/projects/{project_id}/deployments", tags=["deployments"])
async def list_deployments(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    *,
    environment_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The history, newest first."""
    return await run_in_threadpool(
        lambda: server_of(request).deployments(
            caller, project_id, environment_id=environment_id, limit=limit
        )
    )


@router.post("/projects/{project_id}/deployments", status_code=201, tags=["deployments"])
async def start_deployment(
    project_id: uuid.UUID, body: DeploymentIn, request: Request, caller: Caller
) -> dict[str, Any]:
    """Deploy a ref to an environment: a ``deploy`` run, waiting for approval when the
    environment is production or asks for it."""
    return await run_in_threadpool(
        server_of(request).start_deployment, caller, project_id, body.environment_id, body.ref
    )


__all__ = ["router"]
