"""Test-only run kind and a worker entry point for server tests (S8).

``probe`` runs N steps; each appends its key to a marker file, may sleep, and — when
``AI_CODING_TEST_CRASH`` names the step — kills the worker process (``os._exit``)
the first time it runs, like a power cut in the middle of a task.

``python -m tests.integration.server_support DB_URL DATA_DIR`` runs one worker until
the queue is idle (used to crash a real worker process).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import Field

from ai_coding.api.kinds import KINDS, RunKind, _Params
from ai_coding.api.rbac import Permission
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.domain.enums import ApprovalOperation, Mode, TaskPhase
from ai_coding.orchestrator import HandlerRegistry, StepContext, StepResult, StepSpec

CRASH_ENV = "AI_CODING_TEST_CRASH"
STATS: dict[str, int] = {"current": 0, "peak": 0}
_STATS_LOCK = threading.Lock()  # jobs run in their own threads


class ProbeParams(_Params):
    steps: int = Field(default=1, ge=1, le=10)
    sleep: float = Field(default=0.0, ge=0, le=10)
    block: float = Field(default=0.0, ge=0, le=10)  # synchronous work, like real handlers
    marker: str
    approve: bool = False  # the first step is a deploy: approval required (ТЗ §5)


class ProbeHandler:
    name = "test.probe"
    capabilities = frozenset({"probe.step"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        marker = Path(str(ctx.input["marker"]))
        with marker.open("a", encoding="utf-8") as fh:
            fh.write(f"{ctx.task_key}\n")
        if os.environ.get(CRASH_ENV) == ctx.task_key:
            os._exit(137)
        with _STATS_LOCK:
            STATS["current"] += 1
            STATS["peak"] = max(STATS["peak"], STATS["current"])
        try:
            time.sleep(float(ctx.input.get("block", 0.0)))  # blocks this event loop
            await asyncio.sleep(float(ctx.input["sleep"]))
        finally:
            with _STATS_LOCK:
                STATS["current"] -= 1
        return StepResult(output={"step": ctx.task_key})


def _build(root: Path, config: Any, params: Any) -> Any:
    p = ProbeParams.model_validate(params)
    steps = [
        StepSpec(
            key=f"probe.{i}",
            capability="probe.step",
            phase=TaskPhase.EXECUTION,
            depends_on=(f"probe.{i - 1}",) if i else (),
            max_attempts=3,
            input={"marker": p.marker, "sleep": p.sleep, "block": p.block},
            requires_approval=ApprovalOperation.DEPLOY if p.approve and i == 0 else None,
        )
        for i in range(p.steps)
    ]
    return (HandlerRegistry([ProbeHandler()]), steps), p.model_dump()


def register() -> None:
    KINDS["probe"] = RunKind(
        "probe", Mode.AUDIT, Permission.RUN_CREATE, ProbeParams, _build, "test probe"
    )


if __name__ == "__main__":
    register()
    url, data = sys.argv[1], Path(sys.argv[2])
    server = Server(ServerSettings(data_dir=data, database_url=url))
    worker = Worker(server, concurrency=2, poll_seconds=0.1, lease_seconds=0.6)
    asyncio.run(worker.run_until_idle(timeout=60))
    server.close()
