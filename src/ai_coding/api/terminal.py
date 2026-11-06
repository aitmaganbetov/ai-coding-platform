"""Browser terminals over WebSockets, and the workspace service (ADR-0036).

Protocol (one WebSocket per terminal tab):

* client → server, first message: ``{"token": …}`` to the API or ``{"ticket": …}`` to
  the workspace service, plus ``"cols"``/``"rows"``. Browsers cannot set headers on a
  WebSocket, and a token in the URL would end up in logs.
* then ``{"type": "input", "data": "…"}`` and ``{"type": "resize", "cols": …, "rows": …}``;
* server → client: binary frames (terminal output) and ``{"type": "error"|"exit", …}``.

The workspace service (``ai-coding server workspaces``) runs where Docker is reachable
(next to the worker in docker-compose); the API, which faces the Internet, has no Docker
access and reaches it with tickets signed by ``AI_CODING_SERVER_WORKSPACE_SECRET``. The
service checks the user's role itself: a ticket only vouches for who is asking.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from ai_coding.api.core import ApiError
from ai_coding.api.workspace_broker import (
    BrokerError,
    LocalBroker,
    TerminalSession,
    WorkspaceRef,
    verify_ticket,
)
from ai_coding.observability import get_logger

_log = get_logger(__name__)
MAX_INPUT = 64 * 1024
FIRST_MESSAGE_SECONDS = 15.0


def terminal_size(message: dict[str, Any]) -> tuple[int, int]:
    def clamp(value: Any, default: int) -> int:
        try:
            return max(2, min(int(value), 1000))
        except (TypeError, ValueError):
            return default

    return clamp(message.get("cols"), 120), clamp(message.get("rows"), 32)


async def first_message(websocket: WebSocket) -> dict[str, Any]:
    raw = await asyncio.wait_for(websocket.receive_text(), FIRST_MESSAGE_SECONDS)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("the first message must be an object")
    return data


async def refuse(websocket: WebSocket, message: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_text(json.dumps({"type": "error", "message": message}))
        await websocket.close(code=1008)


async def bridge(
    websocket: WebSocket, session: TerminalSession, *, max_input: int = MAX_INPUT
) -> None:
    """Pump terminal output to the browser and keystrokes back until either side ends.
    The workspace service also carries the chat's JSON lines this way (``max_input``)."""

    async def to_browser() -> None:
        try:
            async for chunk in session.output():
                await websocket.send_bytes(chunk)
            await websocket.send_text(json.dumps({"type": "exit"}))
        except BrokerError as exc:
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))

    async def from_browser() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not text or len(text) > max_input * 2:
                continue
            try:
                control = json.loads(text)
            except ValueError:
                continue
            if not isinstance(control, dict):
                continue
            if control.get("type") == "input" and isinstance(control.get("data"), str):
                await session.send(control["data"][:max_input].encode())
            elif control.get("type") == "resize":
                await session.resize(*terminal_size(control))

    tasks = {asyncio.create_task(to_browser()), asyncio.create_task(from_browser())}
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
    finally:
        await session.close()
        with contextlib.suppress(Exception):
            await websocket.close()


CHAT_OPS = frozenset({"hello", "new", "open", "close", "delete", "send", "approve", "interrupt",
                      "login", "usage"})  # fmt: skip
MAX_CHAT_MESSAGE = 512 * 1024


async def chat_bridge(websocket: WebSocket, session: TerminalSession) -> None:
    """The browser's chat panel <-> the chat daemon of the user's container: each event
    is a text frame (one JSON object); the browser may only send the chat's operations."""

    async def to_browser() -> None:
        try:
            async for line in session.output():
                if line.strip():
                    await websocket.send_text(line.decode(errors="replace"))
            await websocket.send_text(json.dumps({"t": "closed"}))
        except BrokerError as exc:
            await websocket.send_text(json.dumps({"t": "error", "message": str(exc)}))

    async def from_browser() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not text or len(text) > MAX_CHAT_MESSAGE:
                continue
            try:
                request = json.loads(text)
            except ValueError:
                continue
            if isinstance(request, dict) and request.get("op") in CHAT_OPS:
                await session.send(json.dumps(request, ensure_ascii=False).encode())

    tasks = {asyncio.create_task(to_browser()), asyncio.create_task(from_browser())}
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
    finally:
        await session.close()
        with contextlib.suppress(Exception):
            await websocket.close()


# ------------------------------------------------------------------ service --
def create_workspace_app(server: Any, broker: LocalBroker) -> FastAPI:
    """The workspace service: terminals and git of workspaces, for the API only."""
    secret = server.settings.workspace_secret
    if not secret:
        raise ValueError("AI_CODING_SERVER_WORKSPACE_SECRET is required by the workspace service")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        reaper = asyncio.create_task(broker.run(stop))
        try:
            yield
        finally:
            stop.set()
            await reaper

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    def resolve(ticket: Any) -> WorkspaceRef:
        project_id, user_id = verify_ticket(secret, str(ticket))
        principal = server.principal_of(user_id)
        ref: WorkspaceRef = server.workspace_ref(principal, project_id)
        return ref

    @app.exception_handler(BrokerError)
    async def broker_error(_request: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse({"message": str(exc)}, status_code=400)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/terminal")
    async def terminal(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            first = await first_message(websocket)
            ref = await run_in_threadpool(resolve, first.get("ticket", ""))
            session = await broker.attach(ref, *terminal_size(first))
        except (BrokerError, ApiError, ValueError, TypeError, TimeoutError) as exc:
            await refuse(websocket, getattr(exc, "message", str(exc)))
            return
        await bridge(websocket, session)

    @app.websocket("/chat")
    async def chat(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            first = await first_message(websocket)
            ref = await run_in_threadpool(resolve, first.get("ticket", ""))
            session = await broker.chat(ref)
        except (BrokerError, ApiError, ValueError, TypeError, TimeoutError) as exc:
            await refuse(websocket, getattr(exc, "message", str(exc)))
            return
        await bridge(websocket, session, max_input=MAX_CHAT_MESSAGE)

    @app.post("/git")
    async def git(request: Request) -> Response:
        body = await request.json()
        ref = await run_in_threadpool(resolve, body.get("ticket", ""))
        message, patch, path = body.get("message"), body.get("patch"), body.get("path")
        try:
            outcome = await broker.git(
                ref, str(body.get("op", "")), str(message) if message is not None else None,
                patch=str(patch) if patch is not None else None,
                path=str(path) if path is not None else None,
            )  # fmt: skip
        except BrokerError as exc:
            return JSONResponse({"message": str(exc)}, status_code=400)
        if outcome.bundle is not None:
            return Response(outcome.bundle, media_type="application/octet-stream",
                            headers={"X-Branch": outcome.branch or ""})  # fmt: skip
        return JSONResponse({"ok": outcome.ok, "output": outcome.output, "branch": outcome.branch})

    @app.post("/stop")
    async def stop(request: Request) -> dict[str, str]:
        body = await request.json()
        await broker.stop(await run_in_threadpool(resolve, body.get("ticket", "")))
        return {"status": "stopped"}

    return app


def parse_status(output: str) -> dict[str, Any]:
    """``git status --porcelain=v1 --branch`` → branch and changed files."""
    branch = None
    changed = []
    for line in output.splitlines():
        if line.startswith("## "):
            head = line[3:]
            if head.startswith("No commits yet on "):
                branch = head.removeprefix("No commits yet on ")
            elif not head.startswith("HEAD (no branch)"):
                branch = head.split("...", 1)[0].split(" ", 1)[0]
        elif len(line) > 3:
            changed.append({"status": line[:2], "path": line[3:]})
    return {"branch": branch, "files": changed}


__all__ = [
    "CHAT_OPS",
    "bridge",
    "chat_bridge",
    "create_workspace_app",
    "first_message",
    "parse_status",
    "refuse",
    "terminal_size",
]
