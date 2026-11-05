"""Implement → test → review loop shared by DEVELOP and MODERNIZE (FR-04, FR-05, B-503).

One iteration:

1. the Developer Agent proposes a change set (full file contents + tests) for the
   issue, given the relevant files and the feedback of the previous iteration;
2. the change set is applied to a *scratch copy* of the base tree (an export of the
   base commit — the repository and the user's working copy are never touched);
3. the project tests run in the Docker sandbox on that copy;
4. the Reviewer Agent reviews the diff (advisory), the Database Agent analyses
   migrations if any were touched.

The loop stops when the tests pass (or are honestly SKIPPED, outside strict mode)
and the review has no blocking finding; otherwise the failures become feedback for
the next iteration. ``max_iterations`` and the run budget bound the loop; the result
of every iteration is kept as an artifact. Only the accepted change set is ever
committed, by :func:`commit_change_set` (an idempotent effect, see ``develop.py``).
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ai_coding.agents import (
    DatabaseAgent,
    DeveloperAgent,
    ReviewerAgent,
    blocking_findings,
    is_migration_path,
)
from ai_coding.agents.base import unsafe_path
from ai_coding.domain.enums import ApprovalOperation, ExecutionBackend
from ai_coding.executors import DockerSandboxExecutor
from ai_coding.executors.base import Executor
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator.context import ContextManager
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.policy import Decision, Verdict
from ai_coding.orchestrator.registry import StepContext
from ai_coding.security import redact
from ai_coding.tools.testrun import SandboxTestRunner, TestRun, TestStatus, plan_tests
from ai_coding.workflows.agent_runtime import run_agent

CONTEXT_TOKENS = 24_000
CONTEXT_FILES = 15
MAX_LISTED_FILES = 2_000
_OUTPUT_FEEDBACK = 4_000


@dataclass(frozen=True, slots=True)
class ImplementSettings:
    """Knobs of the loop (CLI / config)."""

    max_iterations: int = 3
    require_tests: bool = True
    strict: bool = False  # SKIPPED tests are not acceptable
    review: bool = True


@dataclass
class ImplementResult:
    """Accepted change set and the evidence that led to it."""

    changes: list[dict[str, Any]]
    summary: str
    plan: list[str]
    tests: dict[str, Any]
    review: dict[str, Any] | None
    database: dict[str, Any] | None
    diff: str
    iterations: list[dict[str, Any]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def manifest(self) -> list[dict[str, Any]]:
        """Paths, actions and hashes (no contents) for outputs and verification."""
        return [
            {
                "path": c["path"],
                "action": c["action"],
                "sha256": hashlib.sha256(c["content"].encode()).hexdigest()
                if c.get("content") is not None else None,
            }
            for c in self.changes
        ]  # fmt: skip


# ----------------------------------------------------------------- helpers --
def apply_changes(root: Path, changes: Sequence[Mapping[str, Any]]) -> None:
    """Write a validated change set into ``root`` (exact bytes, no CRLF conversion)."""
    for c in changes:
        why = unsafe_path(c["path"])
        if why:
            raise PermanentError(f"refusing to write {c['path']}: {why}")
        target = root / c["path"]
        if c["action"] == "delete":
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(str(c["content"]).encode("utf-8"))


def merge_changes(
    first: Sequence[Mapping[str, Any]], second: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """``first`` then ``second``; a later change of the same path wins."""
    merged = {str(c["path"]): dict(c) for c in first}
    merged.update({str(c["path"]): dict(c) for c in second})
    return list(merged.values())


def unified_diff(base: Path, changes: Sequence[Mapping[str, Any]]) -> str:
    """Unified diff of the change set against the base tree."""
    out: list[str] = []
    for c in changes:
        path = c["path"]
        old_file = base / path
        old = old_file.read_text(encoding="utf-8", errors="replace") if old_file.is_file() else ""
        new = "" if c["action"] == "delete" else str(c.get("content") or "")
        out += difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="/dev/null" if c["action"] == "create" else f"a/{path}",
            tofile="/dev/null" if c["action"] == "delete" else f"b/{path}",
        )
    return "".join(line if line.endswith("\n") else line + "\n" for line in out)


def authorize_change_set(ctx: StepContext, changes: Sequence[Mapping[str, Any]]) -> None:
    """Deletions and critical files need approval; out-of-scope writes are denied."""
    critical: list[str] = []
    for c in changes:
        decision = ctx.policy.file_write(c["path"])
        if decision.verdict is Verdict.DENY:
            ctx.authorize(decision)
        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            critical.append(c["path"])
    deletes = [c["path"] for c in changes if c["action"] == "delete"]
    if deletes:
        decision = ctx.policy.operation(ApprovalOperation.DELETE)
        if not decision.allowed:
            ctx.authorize(Decision(decision.verdict, decision.rule,
                                   "change set deletes files: " + ", ".join(deletes),
                                   decision.operation))  # fmt: skip
    if critical:
        ctx.authorize(Decision(
            Verdict.REQUIRE_APPROVAL, "approvals.critical_paths",
            "change set touches critical files: " + ", ".join(critical),
            ApprovalOperation.CRITICAL_FILE_CHANGE,
        ))  # fmt: skip


def commit_change_set(
    repo: LocalGitRepository,
    *,
    branch: str,
    base: str,
    changes: Sequence[Mapping[str, Any]],
    message: str,
    trailers: Mapping[str, str],
    worktree: Path,
) -> str:
    """Write ``changes`` on a new ``branch`` from ``base`` in a worktree and commit once."""
    if repo.branch_exists(branch):
        raise PermanentError(f"branch '{branch}' already exists; choose another --branch")
    wt = repo.add_worktree(worktree, branch, base=base)
    try:
        apply_changes(wt.path, changes)
        return LocalGitRepository(wt.path).commit(message, trailers=dict(trailers))
    finally:
        repo.remove_worktree(wt.path, force=True)
        shutil.rmtree(worktree, ignore_errors=True)


def test_runner(ctx: StepContext, executor: Executor | None = None) -> SandboxTestRunner:
    """Sandbox runner configured from the project policy."""
    cfg = ctx.config.execution
    ctx.authorize(
        ctx.policy.tool("tests", executes_project_code=True, backend=ExecutionBackend.DOCKER)
    )
    return SandboxTestRunner(
        executor or DockerSandboxExecutor(limits=cfg.limits, docker_config=cfg.docker),
        network_allowed=lambda host: ctx.policy.network(host).allowed,
        sandbox_network=cfg.docker.network == "bridge",
    )


async def run_tests(runner: SandboxTestRunner, root: Path, ctx: StepContext) -> TestRun:
    """Tests for ``root`` or an honest SKIPPED when none exist / Docker is unavailable."""
    plan = plan_tests(root, ctx.config.quality_gates.tests)
    if plan is None:
        return TestRun(TestStatus.SKIPPED, "no test setup detected", "-")
    if not ctx.config.quality_gates.tests.enabled:
        return TestRun(TestStatus.SKIPPED, "tests gate disabled", plan.describe())
    if not runner.executor.available():
        return TestRun(TestStatus.SKIPPED, "Docker sandbox unavailable", plan.describe())
    present = getattr(runner.executor, "image_present", None)
    if present is not None and not await asyncio.to_thread(present, plan.image):
        return TestRun(TestStatus.SKIPPED, f"sandbox image {plan.image.split('@')[0]} is not "
                       "present (run `ai-coding tools pull`)", plan.describe())  # fmt: skip
    return await runner.run(root, plan)


def tests_acceptable(run: TestRun, strict: bool) -> bool:
    """PASSED always; SKIPPED only outside strict mode."""
    return run.ok or (run.status is TestStatus.SKIPPED and not strict)


# -------------------------------------------------------------------- loop --
@dataclass(frozen=True, slots=True)
class _Attempt:
    n: int
    base: Path
    dev: Mapping[str, Any]
    changes: list[dict[str, Any]]


class ImplementLoop:
    """Runs the iterations for one issue against an exported base tree."""

    def __init__(
        self,
        ctx: StepContext,
        *,
        settings: ImplementSettings,
        executor: Executor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.executor = executor
        self.transport = transport
        self.env = env

    async def _agent(self, agent: Any, inputs: Mapping[str, Any]) -> tuple[Any, Any]:
        return await run_agent(self.ctx, agent, inputs, transport=self.transport, env=self.env)

    def _context(self, base: Path, query: str, extra: Sequence[str]) -> list[dict[str, Any]]:
        manager = ContextManager(base)
        bundle = manager.select(query, max_tokens=CONTEXT_TOKENS, max_files=CONTEXT_FILES)
        items = [{"path": i.path, "content": i.text, "truncated": i.truncated}
                 for i in bundle.items]  # fmt: skip
        known = {i["path"] for i in items if not i["truncated"]}
        for path in extra:  # hints may come from agent-made plans: same rules as the index
            file = base / path
            if (path in known or unsafe_path(path) or manager.is_excluded(path)
                    or file.is_symlink() or not file.is_file()):  # fmt: skip
                continue
            items = [i for i in items if i["path"] != path]
            text = redact(file.read_text(encoding="utf-8", errors="replace"))
            items.append({"path": path, "content": text, "truncated": False})
        return items

    async def run(
        self,
        base_ref: str,
        issue: Mapping[str, Any],
        *,
        hint_files: Sequence[str] = (),
        seed: Sequence[Mapping[str, Any]] = (),
    ) -> ImplementResult:
        """Iterate until accepted or ``max_iterations`` is exhausted.

        ``seed`` are deterministic changes (e.g. a package manager's lock file update)
        applied before the first iteration; they are part of the accepted change set.
        """
        repo = LocalGitRepository.open(self.ctx.project_root)
        runner = test_runner(self.ctx, self.executor)
        with tempfile.TemporaryDirectory(prefix="ai-coding-base-") as tmp:
            base = Path(tmp) / "base"
            repo.export(base_ref, base)
            apply_changes(base, seed)
            files = repo.list_files(base_ref)
            query = f"{issue.get('title', '')}\n{issue.get('body', '')}"
            context = self._context(base, query, hint_files)
            feedback: dict[str, Any] | None = None
            history: list[dict[str, Any]] = []
            models: list[str] = []
            for n in range(1, self.settings.max_iterations + 1):
                dev_inputs = {
                    "issue": dict(issue), "files": files[:MAX_LISTED_FILES], "context": context,
                    "feedback": feedback, "require_tests": self.settings.require_tests,
                }  # fmt: skip
                dev_ctx, dev = await self._agent(DeveloperAgent(), dev_inputs)
                changes = dev.output["changes"]
                unseen = [c["path"] for c in changes if c["action"] == "modify" and not any(
                    i["path"] == c["path"] and not i["truncated"] for i in context)]  # fmt: skip
                if unseen:  # the agent rewrote a file it never saw in full: show it, retry
                    context = self._context(base, query, [*hint_files, *unseen])
                    dev_inputs = {**dev_inputs, "context": context}
                    dev_ctx, dev = await self._agent(DeveloperAgent(), dev_inputs)
                    changes = dev.output["changes"]
                models += dev_ctx.models
                record, accepted, feedback = await self._evaluate(
                    _Attempt(n, base, dev.output, changes), runner, issue, models
                )
                history.append(record)
                self.ctx.save_artifact(f"iteration-{n}.json",
                                       json.dumps(record, indent=2, ensure_ascii=False).encode(),
                                       "application/json")  # fmt: skip
                if accepted:
                    return ImplementResult(
                        changes=merge_changes(seed, changes), summary=dev.output["summary"],
                        plan=dev.output["plan"],
                        tests=record["tests"], review=record["review"],
                        database=record["database"], diff=record.pop("_diff"),
                        iterations=history, models=list(dict.fromkeys(models)),
                    )  # fmt: skip
                record.pop("_diff", None)
        reasons = "; ".join(history[-1]["problems"]) if history else "no iterations"
        raise PermanentError(
            f"change not accepted after {self.settings.max_iterations} iteration(s): {reasons}"
        )

    async def _evaluate(
        self,
        attempt: _Attempt,
        runner: SandboxTestRunner,
        issue: Mapping[str, Any],
        models: list[str],
    ) -> tuple[dict[str, Any], bool, dict[str, Any]]:
        n, base, dev, changes = attempt.n, attempt.base, attempt.dev, attempt.changes
        with tempfile.TemporaryDirectory(prefix="ai-coding-try-") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(base, work)
            apply_changes(work, changes)
            diff = unified_diff(base, changes)
            tests = await run_tests(runner, work, self.ctx)
            line_counts = {
                c["path"]: max(1, len(str(c.get("content") or "").splitlines()))
                for c in changes if c["action"] != "delete"
            }  # fmt: skip
            review: dict[str, Any] | None = None
            if self.settings.review:
                rev_ctx, rev = await self._agent(ReviewerAgent(), {
                    "issue": dict(issue), "diff": diff, "changed_files": line_counts,
                    "tests": {k: v for k, v in tests.to_dict().items() if k != "output"},
                    "standards": {"stacks": [s.value for s in self.ctx.config.stacks]},
                })  # fmt: skip
                review = dict(rev.output)
                models += rev_ctx.models
            migrations = [{"path": c["path"], "content": c.get("content") or ""}
                          for c in changes if is_migration_path(c["path"])]  # fmt: skip
            database: dict[str, Any] | None = None
            if migrations:
                db_ctx, db = await self._agent(DatabaseAgent(), {
                    "migrations": migrations, "diff": diff,
                    "stack": [s.value for s in self.ctx.config.stacks],
                })  # fmt: skip
                database = dict(db.output)
                models += db_ctx.models
        problems: list[str] = []
        if not tests_acceptable(tests, self.settings.strict):
            problems.append(f"tests {tests.status.value}: {tests.reason}")
        blocking = blocking_findings(review or {})
        if blocking:
            problems.append(f"review: {len(blocking)} critical/high finding(s)")
        last = n == self.settings.max_iterations
        policy_blocks = self.ctx.config.quality_gates.review.ai_review_blocking
        # Advisory review: blocking findings drive the fix loop, but after the last
        # iteration they block only under an explicit policy (ТЗ §10).
        accepted = tests_acceptable(tests, self.settings.strict) and (
            not blocking or (last and not policy_blocks)
        )
        record = {
            "iteration": n,
            "summary": dev["summary"],
            "changes": [{"path": c["path"], "action": c["action"]} for c in changes],
            "tests": tests.to_dict(),
            "review": review,
            "database": database,
            "problems": problems,
            "accepted": accepted,
            "_diff": diff,
        }
        feedback = {
            "tests": {"status": tests.status.value, "reason": tests.reason,
                      "output": tests.output[-_OUTPUT_FEEDBACK:]},
            "blocking_review_findings": blocking,
            "previous_changes": record["changes"],
        }  # fmt: skip
        return record, accepted, feedback
