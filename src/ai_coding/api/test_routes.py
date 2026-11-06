"""HTTP routes: Per-test results (ADR-0044).
Registered by :func:`ai_coding.api.app.create_app`."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from ai_coding.api.app import API, Caller, server_of

router = APIRouter(prefix=API)


@router.get("/runs/{run_id}/tests", tags=["runs"])
async def run_tests(run_id: uuid.UUID, request: Request, caller: Caller) -> dict[str, Any]:
    """Per-test results of a run from its JUnit reports (empty, with the reason, when the
    run left none)."""
    return await run_in_threadpool(server_of(request).run_tests, caller, run_id)


@router.get("/projects/{project_id}/test-runs", tags=["runs"])
async def project_test_runs(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    limit: int = Query(default=15),  # clamped to 1..50 after the access check
) -> list[dict[str, Any]]:
    """The project's recent runs that run tests: totals, failing tests, or why none ran."""
    return await run_in_threadpool(server_of(request).project_test_runs, caller, project_id, limit)


@router.get("/projects/{project_id}/tests/history", tags=["runs"])
async def test_history(
    project_id: uuid.UUID,
    request: Request,
    caller: Caller,
    test: str | None = Query(default=None, max_length=1000),
) -> list[dict[str, Any]]:
    """One test (``classname::name``) across the project's recent runs (``test`` is
    checked after access: an outsider gets 404 whatever the query)."""
    return await run_in_threadpool(server_of(request).test_history, caller, project_id, test)


__all__ = ["router"]
