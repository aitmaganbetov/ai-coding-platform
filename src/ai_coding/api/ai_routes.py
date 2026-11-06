"""HTTP routes: AI provider settings (ADR-0046).
Registered by :func:`ai_coding.api.app.create_app`."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ai_coding.api.app import API, Caller, server_of

router = APIRouter(prefix=API)


class AISettingsIn(BaseModel):
    """The ``ai`` section (validated by the service, after the access check)."""

    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=0)


@router.get("/projects/{project_id}/ai", tags=["projects"])
async def get_ai(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """The orchestrator's AI settings: the file's, the web's, the effective ones and the
    source of each; whether a key is set (never the key)."""
    return await run_in_threadpool(server_of(request).ai_settings, caller, project_id)


@router.put("/projects/{project_id}/ai", tags=["projects"])
async def put_ai(
    project_id: uuid.UUID, body: AISettingsIn, request: Request, caller: Caller
) -> dict[str, Any]:
    """Replace the web settings (owners and maintainers; ``version`` guards against
    overwriting a change made meanwhile)."""
    server = server_of(request)
    return await run_in_threadpool(
        server.update_ai_settings, caller, project_id, body.settings, body.version
    )


@router.post("/projects/{project_id}/ai/test", tags=["projects"])
async def test_ai(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """A real call to the configured provider (its model list) with the effective
    settings; the project's network policy applies."""
    return await run_in_threadpool(server_of(request).test_ai, caller, project_id)


@router.get("/projects/{project_id}/ai/models", tags=["projects"])
async def ai_models(project_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """The models the configured provider offers (empty, with a reason, if it lists none)."""
    return await run_in_threadpool(server_of(request).ai_models, caller, project_id)


__all__ = ["router"]
