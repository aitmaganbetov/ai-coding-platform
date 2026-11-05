"""MODERNIZE (FR-05): baseline → impact analysis → migration plan → approval →
one small step per PR → verification → rollback.

Planning run (no ``plan`` given)::

    discover.git → modernize.baseline → modernize.plan → modernize.approve
                 → modernize.step → modernize.verify → [git.push → github.pr → github.pr.verify]

Continuation run (``plan`` taken from an earlier, approved run)::

    discover.git → modernize.baseline → modernize.step → modernize.verify → [PR]

* **baseline** — the tests of the base commit run in the sandbox first. A red
  baseline stops the run (a modernization must not start from failing tests) unless
  explicitly allowed; a green baseline makes passing tests mandatory for every step
  (no regressions, SKIPPED is not enough).
* **impact analysis and plan** — the Planner Agent (modernization role) lists the
  affected files, modules and risks and splits the work into steps that each fit one
  reviewable PR, with their own verification and rollback.
* **approval** — the plan version is approved by a human before any change.
* **step** — the same implement/test/review loop as DEVELOP, on its own work branch,
  committed once (idempotent), verified, then optionally opened as a PR.
* **rollback** — :func:`build_rollback` reverts a step: if its commit was merged
  into the base branch, a revert branch (``git revert``) is created and optionally
  opened as a PR; an unmerged step is rolled back by closing its PR.
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

from ai_coding.agents import MigrationPlannerAgent
from ai_coding.domain.enums import TaskPhase
from ai_coding.executors.base import Executor
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.host import RepositoryHost
from ai_coding.orchestrator.context import ContextManager
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.handlers import GitDiscoveryHandler
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.tools.testrun import TestStatus
from ai_coding.workflows.agent_runtime import run_agent
from ai_coding.workflows.develop import RUN_TRAILER, ImplementHandler, VerifyChangeHandler
from ai_coding.workflows.implement import ImplementSettings, run_tests, test_runner
from ai_coding.workflows.pull_request import PullRequestRequest, build_pull_request

_SLUG = re.compile(r"[^a-z0-9]+")


def plan_version(plan: Mapping[str, Any], previous: str | None = None) -> str:
    """``p<n>-<sha8>`` of a migration plan."""
    n = 1
    if previous and previous.startswith("p"):
        head = previous[1:].split("-", 1)[0]
        n = int(head) + 1 if head.isdigit() else 1
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode())
    return f"p{n}-{digest.hexdigest()[:8]}"


def render_plan(plan: Mapping[str, Any], version: str) -> str:
    """Migration plan as Markdown."""
    lines = [f"# План модернизации `{version}`", "", plan["goal"], "", "## Базовое состояние",
             "", plan["baseline_assessment"], "", "## Влияние", ""]  # fmt: skip
    impact = plan["impact"]
    lines += [f"- Файлы: {', '.join(f'`{f}`' for f in impact['files']) or '—'}",
              f"- Модули: {', '.join(impact['modules']) or '—'}"]  # fmt: skip
    lines += [f"- Риск: {r['risk']} → {r['mitigation']}" for r in impact["risks"]]
    lines += ["", "## Шаги (каждый — отдельный PR)", ""]
    for s in plan["steps"]:
        lines += [f"### {s['id']}: {s['title']} (риск: {s['risk']})", "", s["description"], "",
                  f"- Файлы: {', '.join(f'`{f}`' for f in s['files']) or '—'}",
                  f"- Проверка: {s['verification']}", f"- Откат: {s['rollback']}", ""]  # fmt: skip
    lines += ["## Стратегия отката", "", plan["rollback_strategy"]]
    return "\n".join(lines) + "\n"


def pick_step(plan: Mapping[str, Any], step_id: str | None) -> dict[str, Any]:
    """The requested step, or the first one."""
    steps = list(plan["steps"])
    if step_id is None:
        return dict(steps[0])
    for s in steps:
        if s["id"] == step_id:
            return dict(s)
    known = ", ".join(s["id"] for s in steps)
    raise PermanentError(f"plan has no step {step_id} (steps: {known})")


def next_step_id(plan: Mapping[str, Any], done: str) -> str | None:
    """Id of the step after ``done``."""
    ids = [s["id"] for s in plan["steps"]]
    i = ids.index(done) if done in ids else -1
    return ids[i + 1] if 0 <= i < len(ids) - 1 else None


@dataclass(frozen=True, slots=True)
class ModernizeRequest:
    """What to modernize."""

    goal: str
    base: str = "main"
    plan: Mapping[str, Any] | None = None  # approved plan of an earlier run
    plan_version: str | None = None
    step: str | None = None
    branch: str = ""
    allow_red_baseline: bool = False
    settings: ImplementSettings = field(default_factory=ImplementSettings)

    def branch_for(self, step_id: str) -> str:
        slug = _SLUG.sub("-", self.goal.lower()).strip("-")[:40].strip("-") or "modernize"
        return self.branch or f"ai/modernize-{slug}-{step_id.lower()}"


# ---------------------------------------------------------------- handlers --
class BaselineHandler:
    """``modernize.baseline``: tests of the base commit before any change."""

    name = "builtin.modernize.baseline"
    capabilities = frozenset({"modernize.baseline"})
    cost_usd = 0.0

    def __init__(self, *, executor: Executor | None = None) -> None:
        self._executor = executor

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        repo = LocalGitRepository.open(ctx.project_root)
        base = str(ctx.input["base"])
        commit = repo.rev_parse(base)
        runner = test_runner(ctx, self._executor)
        with tempfile.TemporaryDirectory(prefix="ai-coding-baseline-") as tmp:
            tree = Path(tmp) / "tree"
            await asyncio.to_thread(repo.export, commit, tree)
            tests = await run_tests(runner, tree, ctx)
            shutil.rmtree(tree, ignore_errors=True)
        red = tests.status in {TestStatus.FAILED, TestStatus.ERROR}
        if red and not ctx.input.get("allow_red_baseline"):
            raise PermanentError(
                f"baseline tests are red on {base} ({tests.reason}); fix them first or "
                "pass --allow-red-baseline"
            )
        return StepResult(output={"base": base, "commit": commit, "tests": tests.to_dict()},
                          evidence_refs=[f"git:{commit}"])  # fmt: skip


class MigrationPlanHandler:
    """``modernize.plan``: impact analysis and a stepwise plan (Planner Agent)."""

    name = "agent.planner.migration"
    capabilities = frozenset({"agent.planner.migration"})
    cost_usd = 0.0

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._env = env

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        repo = LocalGitRepository.open(ctx.project_root)
        baseline = ctx.dependencies["modernize.baseline"]
        goal = str(ctx.input["goal"])
        with tempfile.TemporaryDirectory(prefix="ai-coding-plan-") as tmp:
            tree = Path(tmp) / "tree"
            await asyncio.to_thread(repo.export, baseline["commit"], tree)
            bundle = ContextManager(tree).select(goal, max_tokens=24_000, max_files=15)
            context = [{"path": i.path, "content": i.text, "truncated": i.truncated}
                       for i in bundle.items]  # fmt: skip
        files = repo.list_files(baseline["commit"])
        tests = {k: v for k, v in baseline["tests"].items() if k != "output"}
        agent_ctx, result = await run_agent(
            ctx, MigrationPlannerAgent(),
            {"goal": goal, "files": files[:2000], "context": context, "baseline": tests},
            transport=self._transport, env=self._env,
        )  # fmt: skip
        plan = result.output
        version = plan_version(plan, ctx.input.get("previous_version"))
        refs = [
            ctx.save_artifact("migration-plan.json",
                              json.dumps(plan, indent=2, ensure_ascii=False).encode(),
                              "application/json").uri,
            ctx.save_artifact("migration-plan.md", render_plan(plan, version).encode(),
                              "text/markdown").uri,
        ]  # fmt: skip
        return StepResult(
            output={"plan": plan, "version": version,
                    "models": list(dict.fromkeys(agent_ctx.models))},
            evidence_refs=refs + list(result.evidence_refs),
            confidence=result.confidence,
        )  # fmt: skip


class PlanApprovalHandler:
    """``modernize.approve``: a human approves the exact plan version."""

    name = "builtin.modernize.approve"
    capabilities = frozenset({"modernize.approve"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        version = str(ctx.dependencies["modernize.plan"]["version"])
        ctx.authorize(ctx.policy.plan_approval(version))
        return StepResult(output={"approved_version": version})


class ModernizeStepHandler(ImplementHandler):
    """``modernize.step``: one plan step through the implement loop, then a commit."""

    name = "builtin.modernize.step"
    capabilities = frozenset({"modernize.step"})

    def _plan(self, ctx: StepContext) -> tuple[dict[str, Any], str]:
        if "modernize.plan" in ctx.dependencies:
            planned = ctx.dependencies["modernize.plan"]
            return dict(planned["plan"]), str(planned["version"])
        return dict(ctx.input["plan"]), str(ctx.input["plan_version"])

    def prepare(self, ctx: StepContext) -> tuple[dict[str, Any], list[str], ImplementSettings]:
        plan, _ = self._plan(ctx)
        step = pick_step(plan, ctx.input.get("step"))
        body = (
            f"Modernization goal: {plan['goal']}\n\nStep {step['id']}: {step['description']}\n\n"
            f"Verification: {step['verification']}\nFiles: {', '.join(step['files'])}\n"
            "Keep the change limited to this step; the existing tests must keep passing."
        )
        settings = ImplementSettings(**ctx.input["settings"])
        baseline = ctx.dependencies["modernize.baseline"]["tests"]["status"]
        if baseline == TestStatus.PASSED.value:
            settings = replace(settings, strict=True)  # green baseline: no regressions
        return ({"title": f"{step['id']}: {step['title']}", "body": body},
                list(step["files"]), settings)  # fmt: skip

    def extra_output(self, ctx: StepContext) -> dict[str, Any]:
        plan, version = self._plan(ctx)
        step = pick_step(plan, ctx.input.get("step"))
        return {"step": step, "migration_plan": plan, "plan_version": version,
                "next_step": next_step_id(plan, step["id"])}  # fmt: skip


class RevertHandler:
    """``rollback.revert``: revert a merged step on a new branch (FR-05 rollback)."""

    name = "builtin.rollback.revert"
    capabilities = frozenset({"rollback.revert"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        data = ctx.input
        repo = LocalGitRepository.open(ctx.project_root)
        base, branch, target = str(data["base"]), str(data["branch"]), str(data["target_run"])
        merged = repo.commits_with_trailer(RUN_TRAILER, target, base)
        if not merged:
            source = data.get("source_branch")
            pending = bool(source) and repo.branch_exists(str(source))
            advice = f"the change is not merged into {base}: close its PR" + (
                f" and delete branch {source}" if pending else ""
            )
            if data.get("open_pr"):
                raise PermanentError(f"nothing to revert — {advice}")
            return StepResult(output={"status": "not_merged", "reverted": [], "advice": advice})
        run_tag = str(ctx.run_id)

        def probe() -> dict[str, Any] | None:
            if not repo.branch_exists(branch):
                return None
            if repo.commits_with_trailer("AI-Coding-Revert", run_tag, branch):
                return {"commit": repo.rev_parse(branch)}
            return None

        def revert() -> dict[str, Any]:
            if repo.branch_exists(branch):
                raise PermanentError(f"branch '{branch}' already exists")
            path = ctx.project_root / ".ai-coding" / "worktrees" / run_tag
            wt = repo.add_worktree(path, branch, base=base)
            try:
                work = LocalGitRepository(wt.path)
                for commit in merged:  # newest first
                    work.revert(commit, mainline=1 if work.is_merge(commit) else None,
                                no_commit=True)  # fmt: skip
                sha = work.commit(f"chore: roll back modernization run {target[:12]}",
                                  trailers={"AI-Coding-Revert": run_tag,
                                            "AI-Coding-Reverts": target})  # fmt: skip
            finally:
                repo.remove_worktree(wt.path, force=True)
                shutil.rmtree(path, ignore_errors=True)
            return {"commit": sha}

        done = await asyncio.to_thread(ctx.effect, "revert", revert, probe)
        shas = ", ".join(c[:12] for c in merged)
        body = (f"Откат шага модернизации (прогон `{target}`): `git revert` {shas} "
                f"на `{base}`. Merge выполняет человек.")  # fmt: skip
        return StepResult(
            output={"status": "reverted", "branch": branch, "base": base,
                    "commit": done["commit"], "reverted": merged, "pr_body": body},
            evidence_refs=[f"git:{done['commit']}"],
        )  # fmt: skip


# -------------------------------------------------------------------- plans --
PR = tuple[RepositoryHost, Callable[[], str | None], str]


@dataclass(frozen=True, slots=True)
class _PRSpec:
    branch: str
    base: str
    title: str
    source: str  # step whose output carries ``pr_body``
    after: str  # push only after this step


def _with_pr(handlers: list[Any], steps: list[StepSpec], spec: _PRSpec, pull_request: PR) -> None:
    host, token, api_host = pull_request
    branch, base, title, source, after = spec.branch, spec.base, spec.title, spec.source, spec.after
    pr_registry, pr_steps = build_pull_request(
        PullRequestRequest(branch=branch, base=base, title=title[:250]),
        host, token=token, api_host=api_host,
    )  # fmt: skip
    known = {h.name for h in handlers}
    handlers += [h for h in pr_registry.all() if h.name not in known]
    extra = {"git.push": (after,), "github.pr": (after, source)}
    steps += [
        replace(s, depends_on=(*s.depends_on, *extra.get(s.key, ())),
                input={**s.input, "body_from": source})
        for s in pr_steps if s.key != "discover.git"
    ]  # fmt: skip


def build_modernize(
    request: ModernizeRequest,
    *,
    pull_request: PR | None = None,
    executor: Executor | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a MODERNIZE run."""
    step_id = request.step or (pick_step(request.plan, None)["id"] if request.plan else "M-01")
    branch = request.branch_for(step_id)
    handlers: list[Any] = [
        GitDiscoveryHandler(),
        BaselineHandler(executor=executor),
        ModernizeStepHandler(executor=executor, transport=transport, env=env),
        VerifyChangeHandler(executor=executor, source="modernize.step",
                            name="builtin.modernize.verify", capability="modernize.verify"),
    ]  # fmt: skip
    steps = [
        StepSpec(key="discover.git", capability="discovery.git", phase=TaskPhase.DISCOVERY),
        StepSpec(key="modernize.baseline", capability="modernize.baseline",
                 depends_on=("discover.git",), timeout_seconds=1800,
                 input={"base": request.base, "allow_red_baseline": request.allow_red_baseline}),
    ]  # fmt: skip
    step_input: dict[str, Any] = {
        "base": request.base,
        "branch": branch,
        "step": request.step,
        "settings": {
            "max_iterations": request.settings.max_iterations,
            "require_tests": request.settings.require_tests,
            "strict": request.settings.strict,
            "review": request.settings.review,
        },
        "commit_message": f"refactor: {request.goal}"[:72],
    }
    step_deps: tuple[str, ...] = ("modernize.baseline",)
    if request.plan is None:
        handlers += [MigrationPlanHandler(transport=transport, env=env), PlanApprovalHandler()]
        steps += [
            StepSpec(key="modernize.plan", capability="agent.planner.migration",
                     depends_on=("modernize.baseline",), max_attempts=3,
                     input={"goal": request.goal, "previous_version": request.plan_version}),
            StepSpec(key="modernize.approve", capability="modernize.approve",
                     depends_on=("modernize.plan",)),
        ]  # fmt: skip
        step_deps = ("modernize.baseline", "modernize.plan", "modernize.approve")
    else:
        step_input |= {"plan": dict(request.plan), "plan_version": request.plan_version}
    steps += [
        StepSpec(key="modernize.step", capability="modernize.step", depends_on=step_deps,
                 input=step_input, max_attempts=2, timeout_seconds=3600),
        StepSpec(key="modernize.verify", capability="modernize.verify",
                 depends_on=("modernize.step",), timeout_seconds=1800),
    ]  # fmt: skip
    if pull_request is not None:
        title = f"Modernize ({step_id}): {request.goal}"
        _with_pr(handlers, steps,
                 _PRSpec(branch, request.base, title, "modernize.step", "modernize.verify"),
                 pull_request)  # fmt: skip
    return HandlerRegistry(handlers), steps


@dataclass(frozen=True, slots=True)
class RollbackRequest:
    """Which modernization (or DEVELOP) run to roll back."""

    target_run: str
    base: str = "main"
    source_branch: str | None = None
    branch: str = ""

    @property
    def revert_branch(self) -> str:
        return self.branch or f"ai/revert-{self.target_run[-12:]}"


def build_rollback(
    request: RollbackRequest, *, pull_request: PR | None = None
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a rollback run."""
    handlers: list[Any] = [GitDiscoveryHandler(), RevertHandler()]
    steps = [
        StepSpec(key="discover.git", capability="discovery.git", phase=TaskPhase.DISCOVERY),
        StepSpec(key="rollback.revert", capability="rollback.revert", depends_on=("discover.git",),
                 max_attempts=2, input={"base": request.base, "branch": request.revert_branch,
                                        "target_run": request.target_run,
                                        "source_branch": request.source_branch,
                                        "open_pr": pull_request is not None}),
    ]  # fmt: skip
    if pull_request is not None:
        title = f"Rollback of modernization run {request.target_run[-12:]}"
        _with_pr(handlers, steps,
                 _PRSpec(request.revert_branch, request.base, title, "rollback.revert",
                         "rollback.revert"), pull_request)  # fmt: skip
    return HandlerRegistry(handlers), steps
