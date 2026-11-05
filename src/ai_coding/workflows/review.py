"""AI review of a change (``ai-coding review``, reusable ``ai-review.yml``; ТЗ §10, B-501/B-507).

DAG: ``discover.git → review.run``. The Reviewer Agent sees the diff between ``base``
and ``head`` (untrusted content) and the line counts of the changed files; findings
point at file/line of the new version. The result is **advisory**: the verdict maps
to a ``neutral`` check unless the approved policy
``quality_gates.review.ai_review_blocking`` makes critical/high findings blocking.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ai_coding.agents import ReviewerAgent, blocking_findings
from ai_coding.domain.enums import TaskPhase
from ai_coding.git import GitCommandError, LocalGitRepository
from ai_coding.orchestrator.handlers import GitDiscoveryHandler
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.workflows.agent_runtime import run_agent

MAX_DIFF_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """What to review."""

    base: str
    head: str = "HEAD"
    title: str = ""
    body: str = ""


def conclusion(review: Mapping[str, Any], *, blocking_policy: bool) -> str:
    """Check-run conclusion for a review (advisory by default)."""
    if blocking_findings(review):
        return "failure" if blocking_policy else "neutral"
    return "success" if not review.get("findings") else "neutral"


def render_review(review: Mapping[str, Any], *, base: str, head: str) -> str:
    """Markdown for a PR comment."""
    lines = [f"# AI review — {review['verdict']}", "",
             f"`{base}…{head}` · рекомендательный результат (ТЗ §10)", "",
             review["summary"], ""]  # fmt: skip
    if review["findings"]:
        lines += ["| Severity | Место | Категория | Замечание | Предложение |",
                  "| --- | --- | --- | --- | --- |"]  # fmt: skip
        for f in review["findings"]:
            msg = f["message"].replace("|", "\\|").replace("\n", " ")
            fix = (f.get("suggestion") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {f['severity']} | `{f['file']}:{f['line']}` | {f['category']} | "
                         f"{msg} | {fix} |")  # fmt: skip
    else:
        lines.append("Замечаний нет.")
    return "\n".join(lines) + "\n"


class ReviewHandler:
    """``review.run``."""

    name = "agent.reviewer.diff"
    capabilities = frozenset({"review.run"})
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
        base, head = str(ctx.input["base"]), str(ctx.input["head"])
        base_sha, head_sha = repo.rev_parse(base), repo.rev_parse(head)
        diff = await asyncio.to_thread(repo.diff, base_sha, head_sha)
        truncated = len(diff) > MAX_DIFF_CHARS
        if truncated:
            diff = diff[:MAX_DIFF_CHARS] + "\n… (diff truncated)\n"
        changed: dict[str, int] = {}
        for path, _added, _deleted in await asyncio.to_thread(repo.diff_stat, base_sha, head_sha):
            try:
                text = await asyncio.to_thread(repo.file_at, head_sha, path)
            except GitCommandError:
                continue  # deleted in head
            changed[path] = max(1, len(text.splitlines()))
        if not changed:
            review: dict[str, Any] = {"summary": "No changes to review.", "verdict": "approve",
                                      "findings": []}  # fmt: skip
            models: list[str] = []
            confidence = 1.0
        else:
            issue = {"title": ctx.input.get("title") or "", "body": ctx.input.get("body") or ""}
            agent_ctx, result = await run_agent(ctx, ReviewerAgent(), {
                "issue": issue,
                "diff": diff, "changed_files": changed,
                "standards": {"stacks": [s.value for s in ctx.config.stacks]},
            }, transport=self._transport, env=self._env)  # fmt: skip
            review, models, confidence = dict(result.output), agent_ctx.models, result.confidence
        policy = ctx.config.quality_gates.review.ai_review_blocking
        verdict = conclusion(review, blocking_policy=policy)
        markdown = render_review(review, base=base, head=head)
        refs = [
            ctx.save_artifact("ai-review.json", json.dumps(review, indent=2,
                                                           ensure_ascii=False).encode(),
                              "application/json").uri,
            ctx.save_artifact("ai-review.md", markdown.encode(), "text/markdown").uri,
        ]  # fmt: skip
        return StepResult(
            output={"review": review, "conclusion": verdict, "markdown": markdown,
                    "base": base_sha, "head": head_sha, "changed_files": changed,
                    "diff_truncated": truncated, "models": list(dict.fromkeys(models)),
                    "blocking_policy": policy},
            evidence_refs=refs, confidence=confidence,
        )  # fmt: skip


def build_review(
    request: ReviewRequest,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a review run."""
    handlers: list[Any] = [GitDiscoveryHandler(), ReviewHandler(transport=transport, env=env)]
    steps = [
        StepSpec(key="discover.git", capability="discovery.git", phase=TaskPhase.DISCOVERY),
        StepSpec(key="review.run", capability="review.run", depends_on=("discover.git",),
                 max_attempts=3, input={"base": request.base, "head": request.head,
                                        "title": request.title, "body": request.body}),
    ]  # fmt: skip
    return HandlerRegistry(handlers), steps
