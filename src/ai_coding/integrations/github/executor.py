"""GitHub Actions executor (ТЗ §4: "GitHub Actions adapter"; ADR-0007, ADR-0021).

Runs *named, allow-listed workflows* — never arbitrary commands — on GitHub-hosted
runners:

* ``CommandSpec.argv = ("<workflow>.yml", "key=value", …)``; the values become
  ``workflow_dispatch`` inputs;
* a ``correlation_id`` input is added and the workflow must put it into its
  ``run-name`` (``run-name: ai-coding ${{ inputs.correlation_id }}``) so the run can
  be found without races;
* the run is polled until completion; on timeout it is cancelled;
* artifacts are listed in the summary and, with ``artifacts_dir``, downloaded and
  extracted safely (no zip-slip, no symlinks, size caps).

GitHub Actions is not an offline orchestrator (ТЗ §15): the local orchestrator stays
in charge; this executor only delegates one step.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors.base import CommandSpec, ExecutionResult, ExecutorError
from ai_coding.integrations.github.client import GitHubAPIError
from ai_coding.integrations.github.host import GitHubHost

_INPUT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TERMINAL = "completed"


class GitHubActionsExecutor:
    """Executes allow-listed ``workflow_dispatch`` workflows and waits for the result."""

    backend = ExecutionBackend.GITHUB_ACTIONS

    def __init__(
        self,
        host: GitHubHost,
        *,
        ref: str,
        allowed_workflows: set[str],
        poll_interval: float = 10.0,
        find_timeout: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        correlation: Callable[[], str] = lambda: uuid.uuid4().hex,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.host = host
        self.ref = ref
        self.allowed = frozenset(allowed_workflows)
        self.poll_interval = poll_interval
        self.find_timeout = find_timeout
        self._sleep = sleep
        self._clock = clock
        self._correlation = correlation
        self.artifacts_dir = artifacts_dir

    def available(self) -> bool:
        """Whether the repository is reachable with the configured credentials."""
        try:
            self.host.client.json("GET", f"/repos/{self.host.repo}")
        except (GitHubAPIError, Exception):
            return False
        return True

    @staticmethod
    def _inputs(args: tuple[str, ...]) -> dict[str, str]:
        inputs: dict[str, str] = {}
        for arg in args:
            key, sep, value = arg.partition("=")
            if not sep or not _INPUT_KEY.match(key) or key == "correlation_id":
                raise ExecutorError(f"invalid workflow input {arg!r} (expected key=value)")
            inputs[key] = value
        return inputs

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        """Dispatch, find and await the workflow run."""
        workflow = spec.argv[0]
        if workflow not in self.allowed:
            raise ExecutorError(f"workflow {workflow!r} is not allow-listed for this executor")
        if spec.image is not None or spec.mounts:
            raise ExecutorError("image/mounts are not applicable to GitHub Actions")
        correlation = self._correlation()
        inputs = {**self._inputs(spec.argv[1:]), "correlation_id": correlation}
        started = self._clock()
        await asyncio.to_thread(self.host.dispatch_workflow, workflow, ref=self.ref, inputs=inputs)
        run = await self._find_run(workflow, correlation, started)
        timed_out = False
        while run.get("status") != _TERMINAL:
            if self._clock() - started > spec.timeout_seconds:
                timed_out = True
                await asyncio.to_thread(self.host.cancel_workflow_run, int(run["id"]))
                break
            await self._sleep(self.poll_interval)
            run = await asyncio.to_thread(self.host.workflow_run, int(run["id"]))
        jobs = await asyncio.to_thread(self.host.workflow_jobs, int(run["id"]))
        artifacts = [] if timed_out else await self._artifacts(int(run["id"]))
        summary = {
            "run_id": run["id"],
            "url": run.get("html_url"),
            "conclusion": run.get("conclusion"),
            "jobs": [{"name": j.get("name"), "conclusion": j.get("conclusion")} for j in jobs],
            "artifacts": artifacts,
        }
        failed = [
            j["name"] for j in summary["jobs"] if j["conclusion"] not in {"success", "skipped"}
        ]
        return ExecutionResult(
            backend=self.backend,
            argv=spec.argv,
            exit_code=0 if run.get("conclusion") == "success" and not timed_out else 1,
            stdout=json.dumps(summary),
            stderr="failed jobs: " + ", ".join(str(f) for f in failed) if failed else "",
            duration_seconds=round(self._clock() - started, 3),
            timed_out=timed_out,
        )

    async def _artifacts(self, run_id: int) -> list[dict[str, Any]]:
        """List the run's artifacts; download them when ``artifacts_dir`` is set."""
        listed = await asyncio.to_thread(self.host.workflow_artifacts, run_id)
        out: list[dict[str, Any]] = []
        for art in listed:
            entry: dict[str, Any] = {
                "name": art.get("name"),
                "id": art.get("id"),
                "size_in_bytes": art.get("size_in_bytes"),
                "expired": bool(art.get("expired")),
            }
            if self.artifacts_dir is not None and not entry["expired"]:
                dest = self.artifacts_dir / str(run_id)
                files = await asyncio.to_thread(self.host.download_artifact, art, dest)
                entry["path"] = str(dest / str(art["name"]))
                entry["files"] = len(files)
            out.append(entry)
        return out

    async def _find_run(self, workflow: str, correlation: str, started: float) -> dict[str, Any]:
        while True:
            runs = await asyncio.to_thread(self.host.workflow_runs, workflow, branch=self.ref)
            for run in runs:
                if correlation in str(run.get("display_title", "")) or correlation in str(
                    run.get("name", "")
                ):
                    return run
            if self._clock() - started > self.find_timeout:
                raise ExecutorError(
                    f"dispatched run of {workflow} not found; does its run-name include "
                    "${{ inputs.correlation_id }}?"
                )
            await self._sleep(self.poll_interval)
