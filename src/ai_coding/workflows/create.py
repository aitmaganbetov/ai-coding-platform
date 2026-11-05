"""CREATE mode (FR-03, AC-01, P0-10): idea → requirements → approval → ADR → backlog →
bootstrap → (PR).

DAG::

    execution:    create.requirements → create.approve → create.architecture
                  → create.plan → create.bootstrap
                  → create.verify (before anything is pushed)
    (optional)    git.push → github.pr → github.pr.verify   (see pull_request.py)

* every agent step builds its own routed, guarded LLM from the configuration, so the
  egress policy, redaction, budgets, fallbacks and the response cache apply per agent;
* ``create.approve`` is a mandatory human gate on the *exact* requirements version
  (``spec_approval``) — architecture work never starts on an unapproved version;
* the requirements dialogue is a sequence of runs: each revision takes the previous
  version and the answers to its open questions and produces a new version;
* ``create.bootstrap`` writes the skeleton in a separate worktree on a work branch
  (never on the default branch), asks for approval of critical files (CI, config,
  lock files) and commits once — the commit carries the run id as a trailer, so a
  resumed run finds it instead of committing twice (AC-04);
* ``create.verify`` re-reads every file from the commit and compares hashes with the
  manifest produced by the Bootstrap Agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from ai_coding.agents import (
    ArchitectAgent,
    BootstrapAgent,
    PlannerAgent,
    RequirementsAgent,
)
from ai_coding.agents.architect import render_markdown as architecture_markdown
from ai_coding.agents.planner import render_markdown as plan_markdown
from ai_coding.agents.requirements import render_markdown as requirements_markdown
from ai_coding.domain.enums import ApprovalOperation
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.host import RepositoryHost
from ai_coding.orchestrator.errors import (
    PermanentError,
)
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.policy import Decision, Verdict
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.workflows.agent_runtime import AgentStepHandler
from ai_coding.workflows.pull_request import PullRequestRequest, build_pull_request

RUN_TRAILER = "AI-Coding-Run"
DEFAULT_BRANCH = "ai/bootstrap"


@dataclass(frozen=True, slots=True)
class CreateRequest:
    """What to create."""

    idea: str
    constraints: tuple[str, ...] = ()
    previous: Mapping[str, Any] | None = None  # previous requirements version (revision)
    previous_version: str | None = None
    answers: Mapping[str, str] = field(default_factory=dict)
    project_name: str | None = None
    branch: str = DEFAULT_BRANCH
    base: str = "main"


def spec_version(spec: Mapping[str, Any], previous_version: str | None) -> str:
    """``v<n>-<sha8>``: sequence number of the dialogue round + content hash."""
    n = 1
    if previous_version and previous_version.startswith("v"):
        head = previous_version[1:].split("-", 1)[0]
        n = int(head) + 1 if head.isdigit() else 1
    digest = hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return f"v{n}-{digest[:8]}"


def _save(ctx: StepContext, name: str, text: str, mime: str) -> str:
    return ctx.save_artifact(name, text.encode("utf-8"), mime).uri


class RequirementsStep(AgentStepHandler):
    """``create.requirements``: produces a new, versioned specification."""

    async def run(self, ctx: StepContext) -> StepResult:
        agent_ctx, result = await self.execute(ctx)
        spec = result.output
        version = spec_version(spec, ctx.input.get("previous_version"))
        evidence = [
            _save(ctx, "requirements.json", json.dumps(spec, indent=2, ensure_ascii=False),
                  "application/json"),
            _save(ctx, "requirements.md", requirements_markdown(spec, version=version),
                  "text/markdown"),
        ]  # fmt: skip
        return StepResult(
            output={
                "version": version,
                "spec": spec,
                "open_questions": spec.get("open_questions", []),
                "models": list(dict.fromkeys(agent_ctx.models)),
            },
            evidence_refs=evidence + list(result.evidence_refs),
            confidence=result.confidence,
        )


class SpecApprovalHandler:
    """``create.approve``: the approved version is fixed before design work starts."""

    name = "builtin.create.approve"
    capabilities = frozenset({"create.approve"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        version = str(ctx.dependencies["create.requirements"]["version"])
        ctx.authorize(ctx.policy.spec_approval(version))
        return StepResult(output={"approved_version": version})


class ArchitectureStep(AgentStepHandler):
    """``create.architecture``."""

    async def run(self, ctx: StepContext) -> StepResult:
        agent_ctx, result = await self.execute(ctx)
        arch = result.output
        evidence = [
            _save(ctx, "architecture.json", json.dumps(arch, indent=2, ensure_ascii=False),
                  "application/json"),
            _save(ctx, "architecture.md", architecture_markdown(arch), "text/markdown"),
        ]  # fmt: skip
        return StepResult(
            output={"architecture": arch, "models": list(dict.fromkeys(agent_ctx.models))},
            evidence_refs=evidence + list(result.evidence_refs),
            confidence=result.confidence,
        )


class PlanStep(AgentStepHandler):
    """``create.plan``."""

    async def run(self, ctx: StepContext) -> StepResult:
        agent_ctx, result = await self.execute(ctx)
        plan = result.output
        points = sum(i["estimate_points"] for i in plan["items"])
        evidence = [
            _save(ctx, "backlog.json", json.dumps(plan, indent=2, ensure_ascii=False),
                  "application/json"),
            _save(ctx, "backlog.md", plan_markdown(plan), "text/markdown"),
        ]  # fmt: skip
        return StepResult(
            output={
                "plan": plan,
                "items": len(plan["items"]),
                "story_points": points,
                "models": list(dict.fromkeys(agent_ctx.models)),
            },
            evidence_refs=evidence + list(result.evidence_refs),
            confidence=result.confidence,
        )


class BootstrapStep(AgentStepHandler):
    """``create.bootstrap``: render, approve critical files, commit on the work branch."""

    async def run(self, ctx: StepContext) -> StepResult:
        _, result = await self.execute(ctx)
        files: list[dict[str, Any]] = result.output["files"]
        self._authorize_writes(ctx, [f["path"] for f in files])
        branch, base = str(ctx.input["branch"]), str(ctx.input["base"])
        repo = LocalGitRepository.open(ctx.project_root)
        run_tag = str(ctx.run_id)

        def probe() -> dict[str, Any] | None:
            found = repo.find_commits_by_trailer(RUN_TRAILER, run_tag)
            return {"commit": found[0]} if found else None

        def commit() -> dict[str, Any]:
            if repo.branch_exists(branch):
                raise PermanentError(
                    f"branch '{branch}' already exists; choose another --branch or delete it"
                )
            worktree = ctx.project_root / ".ai-coding" / "worktrees" / run_tag
            wt = repo.add_worktree(worktree, branch, base=base)
            try:
                for f in files:
                    path = wt.path / f["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f["content"].encode("utf-8"))  # exact bytes, no CRLF
                sha = LocalGitRepository(wt.path).commit(
                    f"chore: bootstrap {result.output['package']} (ai-coding create)",
                    trailers={RUN_TRAILER: run_tag},
                )
            finally:
                repo.remove_worktree(wt.path, force=True)
                shutil.rmtree(worktree, ignore_errors=True)
            return {"commit": sha}

        done = await asyncio.to_thread(ctx.effect, "commit", commit, probe)
        manifest = [{k: f[k] for k in ("path", "sha256", "bytes")} for f in files]
        evidence = _save(ctx, "bootstrap-manifest.json",
                         json.dumps(manifest, indent=2), "application/json")  # fmt: skip
        return StepResult(
            output={
                "branch": branch,
                "base": base,
                "commit": done["commit"],
                "stack": result.output["stack"],
                "package": result.output["package"],
                "commands": result.output["commands"],
                "files": manifest,
            },
            evidence_refs=[evidence, f"git:{done['commit']}"],
            confidence=1.0,
        )

    @staticmethod
    def _authorize_writes(ctx: StepContext, paths: list[str]) -> None:
        critical: list[str] = []
        for path in paths:
            decision = ctx.policy.file_write(path)
            if decision.verdict is Verdict.DENY:
                ctx.authorize(decision)
            if decision.verdict is Verdict.REQUIRE_APPROVAL:
                critical.append(path)
        if critical:  # one approval for all critical files; granted → file_write allows
            ctx.authorize(
                Decision(
                    Verdict.REQUIRE_APPROVAL,
                    "approvals.critical_paths",
                    "bootstrap writes critical files: " + ", ".join(critical),
                    ApprovalOperation.CRITICAL_FILE_CHANGE,
                )
            )


class VerifyBootstrapHandler:
    """``create.verify``: the commit contains exactly what the agent produced."""

    name = "builtin.create.verify"
    capabilities = frozenset({"create.verify"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        boot = ctx.dependencies["create.bootstrap"]
        repo = LocalGitRepository.open(ctx.project_root)
        commit = str(boot["commit"])
        if repo.rev_parse(str(boot["branch"])) != commit:
            raise PermanentError("the work branch moved since the bootstrap commit")
        tracked = set(await asyncio.to_thread(repo.list_files, commit))
        problems: list[str] = []
        for f in boot["files"]:
            if f["path"] not in tracked:
                problems.append(f"missing {f['path']}")
                continue
            text = await asyncio.to_thread(repo.file_at, commit, f["path"])
            if hashlib.sha256(text.encode()).hexdigest() != f["sha256"]:
                problems.append(f"content differs: {f['path']}")
        if problems:
            raise PermanentError("bootstrap verification failed: " + "; ".join(problems[:10]))
        return StepResult(
            output={"verified": True, "commit": commit, "files": len(boot["files"])},
            evidence_refs=[f"git:{commit}"],
        )


# -------------------------------------------------------------------- plan --
def _dep(ctx: StepContext, key: str, field_name: str) -> Any:
    return ctx.dependencies[key][field_name]


def build_create(
    request: CreateRequest,
    *,
    pull_request: tuple[RepositoryHost, Callable[[], str | None], str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a CREATE run.

    ``pull_request`` = ``(host, token, api_host, title)`` appends push → PR → verify.
    """
    kw: dict[str, Any] = {"transport": transport, "env": env}
    handlers: list[Any] = [
        RequirementsStep(
            RequirementsAgent(),
            lambda ctx: {
                "idea": ctx.input["idea"],
                "constraints": list(ctx.input.get("constraints") or []),
                "previous": ctx.input.get("previous"),
                "answers": dict(ctx.input.get("answers") or {}),
            },
            **kw,
        ),
        SpecApprovalHandler(),
        ArchitectureStep(
            ArchitectAgent(),
            lambda ctx: {"requirements": _dep(ctx, "create.requirements", "spec")},
            **kw,
        ),
        PlanStep(
            PlannerAgent(),
            lambda ctx: {
                "requirements": _dep(ctx, "create.requirements", "spec"),
                "architecture": _dep(ctx, "create.architecture", "architecture"),
            },
            **kw,
        ),
        BootstrapStep(
            BootstrapAgent(),
            lambda ctx: {
                "requirements": _dep(ctx, "create.requirements", "spec"),
                "requirements_version": _dep(ctx, "create.approve", "approved_version"),
                "architecture": _dep(ctx, "create.architecture", "architecture"),
                "plan": _dep(ctx, "create.plan", "plan"),
                "project_name": ctx.input.get("project_name"),
            },
            **kw,
        ),
        VerifyBootstrapHandler(),
    ]
    agent_attempts = 3  # transient provider failures (all routes unavailable) are retried
    steps = [
        StepSpec(
            key="create.requirements",
            capability="agent.requirements",
            input={
                "idea": request.idea,
                "constraints": list(request.constraints),
                "previous": dict(request.previous) if request.previous else None,
                "previous_version": request.previous_version,
                "answers": dict(request.answers),
            },
            max_attempts=agent_attempts,
        ),
        StepSpec(key="create.approve", capability="create.approve",
                 depends_on=("create.requirements",)),
        StepSpec(key="create.architecture", capability="agent.architect",
                 depends_on=("create.approve", "create.requirements"),
                 max_attempts=agent_attempts),
        StepSpec(key="create.plan", capability="agent.planner",
                 depends_on=("create.architecture", "create.requirements"),
                 max_attempts=agent_attempts),
        StepSpec(
            key="create.bootstrap",
            capability="agent.bootstrap",
            depends_on=("create.plan", "create.approve", "create.requirements",
                        "create.architecture"),
            input={"branch": request.branch, "base": request.base,
                   "project_name": request.project_name},
            max_attempts=2,  # a retry after the commit reuses it (effect ledger + trailer)
        ),
        # Checked before anything is pushed, hence in the execution phase.
        StepSpec(key="create.verify", capability="create.verify",
                 depends_on=("create.bootstrap",)),
    ]  # fmt: skip
    if pull_request is not None:
        host, token, api_host, title = pull_request
        pr_registry, pr_steps = build_pull_request(
            PullRequestRequest(
                branch=request.branch,
                base=request.base,
                title=title,
                body=(
                    "Каркас проекта, созданный AI Coding Platform в режиме CREATE из "
                    "утверждённой версии требований. Требования, архитектура, ADR и "
                    "backlog — в `docs/`."
                ),
            ),
            host,
            token=token,
            api_host=api_host,
        )
        known = {h.name for h in handlers}
        handlers += [h for h in pr_registry.all() if h.name not in known]
        steps += [
            replace(s, depends_on=(*s.depends_on, "create.verify")) if s.key == "git.push" else s
            for s in pr_steps
        ]
    return HandlerRegistry(handlers), steps
