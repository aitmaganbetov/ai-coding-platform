"""DEVELOP (FR-04, AC-03, P0-09): issue → plan → branch → patch → tests → review → PR.

DAG::

    discover.git → develop.implement → develop.verify → [git.push → github.pr → github.pr.verify]

* ``develop.implement`` runs the implement/test/review loop (``implement.py``) on a
  scratch copy of the base commit, then commits the accepted change set once on a new
  work branch in a separate worktree — never on the default branch, never in the
  user's working copy. Deletions and critical files require approval. The commit
  carries the run id as a trailer; a retry or resume finds it instead of committing
  twice (AC-04).
* ``develop.verify`` re-reads every committed file and compares its hash with the
  accepted change set, then re-runs the tests on the committed tree in the sandbox:
  the PR is opened only for what was actually verified.
* the PR body carries the summary, the test result and the advisory review findings;
  the platform never merges.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from ai_coding.domain.enums import TaskPhase
from ai_coding.executors.base import Executor
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.host import RepositoryHost
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.handlers import GitDiscoveryHandler
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.tools.testrun import TestStatus
from ai_coding.workflows.implement import (
    ImplementLoop,
    ImplementSettings,
    authorize_change_set,
    commit_change_set,
    run_tests,
    test_runner,
)
from ai_coding.workflows.pull_request import PullRequestRequest, build_pull_request

RUN_TRAILER = "AI-Coding-Run"
_SLUG = re.compile(r"[^a-z0-9]+")


def branch_for(title: str, number: int | None = None, prefix: str = "ai") -> str:
    """``ai/<number>-<slug>`` — a safe, readable work branch name."""
    slug = _SLUG.sub("-", title.lower()).strip("-")[:40].strip("-") or "change"
    return f"{prefix}/{number}-{slug}" if number else f"{prefix}/{slug}"


@dataclass(frozen=True, slots=True)
class DevelopRequest:
    """What to implement."""

    title: str
    body: str = ""
    issue_number: int | None = None
    branch: str = ""
    base: str = "main"
    settings: ImplementSettings = field(default_factory=ImplementSettings)
    hint_files: tuple[str, ...] = ()

    @property
    def work_branch(self) -> str:
        return self.branch or branch_for(self.title, self.issue_number)


def pr_body(output: Mapping[str, Any], issue_number: int | None) -> str:
    """PR description: summary, tests, advisory review, traceability."""
    tests = output["tests"]
    lines = [output["summary"], "", "## Изменения", ""]
    lines += [f"- `{c['path']}` ({c['action']})" for c in output["changes"]]
    lines += ["", "## Тесты", "",
              f"{tests['status'].upper()} — {tests['reason']} (`{tests['command']}`)"]  # fmt: skip
    if tests.get("passed") is not None:
        lines.append(f"passed: {tests['passed']}, failed: {tests['failed']}")
    review = output.get("review")
    if review:
        lines += ["", f"## AI review (рекомендательно): {review['verdict']}", "",
                  review["summary"]]  # fmt: skip
        lines += [f"- **{f['severity']}** `{f['file']}:{f['line']}` — {f['message']}"
                  for f in review["findings"]]  # fmt: skip
    if output.get("database"):
        lines += ["", "## Database Agent", "", output["database"]["summary"]]
    if output.get("step"):
        step = output["step"]
        lines += [
            "", f"## Шаг модернизации {step['id']}: {step['title']}", "",
            f"Проверка: {step['verification']}", "", f"Откат: {step['rollback']}",
        ]  # fmt: skip
    lines += ["", f"Итераций: {output['iterations']}. Merge выполняет человек."]
    if issue_number:
        lines += ["", f"Closes #{issue_number}"]
    return "\n".join(lines)


class ImplementHandler:
    """``develop.implement``: loop on a scratch copy, then one idempotent commit."""

    name = "builtin.develop.implement"
    capabilities = frozenset({"develop.implement"})
    cost_usd = 0.0

    def __init__(
        self,
        *,
        executor: Executor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._executor = executor
        self._transport = transport
        self._env = env

    def is_available(self) -> bool:
        return True

    def prepare(self, ctx: StepContext) -> tuple[dict[str, Any], list[str], ImplementSettings]:
        """``(issue, hint_files, settings)`` for this step (overridden by MODERNIZE)."""
        data = ctx.input
        issue = {"title": data["title"], "body": data["body"]}
        return issue, list(data.get("hint_files", ())), ImplementSettings(**data["settings"])

    def extra_output(self, ctx: StepContext) -> dict[str, Any]:
        """Additional output fields (overridden by MODERNIZE)."""
        return {}

    async def run(self, ctx: StepContext) -> StepResult:
        data = ctx.input
        issue, hints, settings = self.prepare(ctx)
        loop = ImplementLoop(ctx, settings=settings, executor=self._executor,
                             transport=self._transport, env=self._env)  # fmt: skip
        result = await loop.run(data["base"], issue, hint_files=hints)
        authorize_change_set(ctx, result.changes)
        repo = LocalGitRepository.open(ctx.project_root)
        run_tag = str(ctx.run_id)
        branch, base = str(data["branch"]), str(data["base"])

        def probe() -> dict[str, Any] | None:
            found = repo.find_commits_by_trailer(RUN_TRAILER, run_tag)
            return {"commit": found[0]} if found else None

        def commit() -> dict[str, Any]:
            message = str(data["commit_message"]) + "\n\n" + result.summary
            sha = commit_change_set(
                repo, branch=branch, base=base, changes=result.changes, message=message,
                trailers={RUN_TRAILER: run_tag},
                worktree=ctx.project_root / ".ai-coding" / "worktrees" / run_tag,
            )  # fmt: skip
            return {"commit": sha}

        done = await asyncio.to_thread(ctx.effect, "commit", commit, probe)
        diff_ref = ctx.save_artifact("change.diff", result.diff.encode(), "text/x-diff")
        report = {
            **self.extra_output(ctx),
            "summary": result.summary,
            "plan": result.plan,
            "changes": result.manifest(),
            "tests": result.tests,
            "review": result.review,
            "database": result.database,
            "iterations": len(result.iterations),
            "models": result.models,
        }
        ref = ctx.save_artifact("implementation.json",
                                json.dumps(report, indent=2, ensure_ascii=False).encode(),
                                "application/json")  # fmt: skip
        body = pr_body(report, data.get("issue_number"))
        return StepResult(
            output={
                **report,
                "branch": branch,
                "base": base,
                "commit": done["commit"],
                "pr_body": body,
            },
            evidence_refs=[ref.uri, diff_ref.uri, f"git:{done['commit']}"],
        )


class VerifyChangeHandler:
    """``develop.verify``: committed content == accepted change set; tests still pass."""

    cost_usd = 0.0

    def __init__(
        self,
        *,
        executor: Executor | None = None,
        source: str = "develop.implement",
        name: str = "builtin.develop.verify",
        capability: str = "develop.verify",
    ) -> None:
        self._executor = executor
        self._source = source
        self.name = name
        self.capabilities = frozenset({capability})

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        impl = ctx.dependencies[self._source]
        repo = LocalGitRepository.open(ctx.project_root)
        commit = str(impl["commit"])
        if repo.rev_parse(str(impl["branch"])) != commit:
            raise PermanentError("the work branch moved since the implementation commit")
        tracked = set(await asyncio.to_thread(repo.list_files, commit))
        problems: list[str] = []
        for c in impl["changes"]:
            if c["action"] == "delete":
                if c["path"] in tracked:
                    problems.append(f"{c['path']} should be deleted")
                continue
            if c["path"] not in tracked:
                problems.append(f"missing {c['path']}")
                continue
            text = await asyncio.to_thread(repo.file_at, commit, c["path"])
            if hashlib.sha256(text.encode()).hexdigest() != c["sha256"]:
                problems.append(f"content differs: {c['path']}")
        if problems:
            raise PermanentError("verification failed: " + "; ".join(problems[:10]))
        runner = test_runner(ctx, self._executor)
        with tempfile.TemporaryDirectory(prefix="ai-coding-verify-") as tmp:
            tree = Path(tmp) / "tree"
            await asyncio.to_thread(repo.export, commit, tree)
            tests = await run_tests(runner, tree, ctx)
            shutil.rmtree(tree, ignore_errors=True)
        before = impl["tests"]["status"]
        if before == TestStatus.PASSED.value and not tests.ok:
            raise PermanentError(f"tests no longer pass on the commit: {tests.reason}")
        return StepResult(
            output={"verified": True, "commit": commit, "tests": tests.to_dict()},
            evidence_refs=[f"git:{commit}"],
        )


def build_develop(
    request: DevelopRequest,
    *,
    pull_request: tuple[RepositoryHost, Callable[[], str | None], str] | None = None,
    executor: Executor | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a DEVELOP run (``pull_request`` = host, token, api_host)."""
    branch = request.work_branch
    handlers: list[Any] = [
        GitDiscoveryHandler(),
        ImplementHandler(executor=executor, transport=transport, env=env),
        VerifyChangeHandler(executor=executor),
    ]
    prefix = f"#{request.issue_number}: " if request.issue_number else ""
    steps = [
        StepSpec(key="discover.git", capability="discovery.git", phase=TaskPhase.DISCOVERY),
        StepSpec(
            key="develop.implement",
            capability="develop.implement",
            depends_on=("discover.git",),
            input={
                "title": request.title,
                "body": request.body,
                "branch": branch,
                "base": request.base,
                "settings": {
                    "max_iterations": request.settings.max_iterations,
                    "require_tests": request.settings.require_tests,
                    "strict": request.settings.strict,
                    "review": request.settings.review,
                },
                "hint_files": list(request.hint_files),
                "commit_message": f"feat: {prefix}{request.title}"[:72],
                "issue_number": request.issue_number,
            },
            max_attempts=2,  # a retry after the commit reuses it (ledger + trailer)
            timeout_seconds=3600,
        ),
        StepSpec(
            key="develop.verify",
            capability="develop.verify",
            depends_on=("develop.implement",),
            timeout_seconds=1800,
        ),
    ]
    if pull_request is not None:
        host, token, api_host = pull_request
        pr_registry, pr_steps = build_pull_request(
            PullRequestRequest(branch=branch, base=request.base,
                               title=f"{prefix}{request.title}"[:250], body=""),
            host, token=token, api_host=api_host,
        )  # fmt: skip
        known = {h.name for h in handlers}
        handlers += [h for h in pr_registry.all() if h.name not in known]
        extra = {
            "git.push": ("develop.verify",),
            "github.pr": ("develop.verify", "develop.implement"),
        }
        steps += [
            replace(s, depends_on=(*s.depends_on, *extra.get(s.key, ())),
                    input={**s.input, "body_from": "develop.implement"})
            for s in pr_steps if s.key != "discover.git"
        ]  # fmt: skip
    return HandlerRegistry(handlers), steps
