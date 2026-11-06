"""HTTP routes: a project's secrets (write-only; ADR-0043, ADR-0046, ADR-0048). Registered by
:func:`ai_coding.api.app.create_app`."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ai_coding.api.app import API, Caller, server_of

router = APIRouter(prefix=API)


class SecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=16 * 1024)


@router.get("/projects/{project_id}/secrets", tags=["projects"])
async def list_secrets(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """Names and hints of the project's secrets (never their values) and whether the
    server can store secrets at all."""
    return await run_in_threadpool(server_of(request).list_secrets, caller, project_id)


@router.put("/projects/{project_id}/secrets/{name}", tags=["projects"])
async def set_secret(
    project_id: uuid.UUID, name: str, body: SecretIn, request: Request, caller: Caller
) -> dict[str, Any]:
    """Store (or replace) a secret, encrypted; the answer has its hint only."""
    return await run_in_threadpool(
        server_of(request).set_secret, caller, project_id, name, body.value
    )


@router.delete("/projects/{project_id}/secrets/{name}", status_code=204, tags=["projects"])
async def delete_secret(
    project_id: uuid.UUID, name: str, request: Request, caller: Caller
) -> Response:
    await run_in_threadpool(server_of(request).delete_secret, caller, project_id, name)
    return Response(status_code=204)


__all__ = ["router"]
