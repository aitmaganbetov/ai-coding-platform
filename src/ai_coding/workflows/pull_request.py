"""Controlled pull-request creation (FR-17, AC-03): push a work branch, open a PR.

DAG::

    discovery:    discover.git
    execution:    git.push  →  github.pr   (approval ``pr_create`` per policy)
    verification: github.pr.verify

* the head branch must not be protected (pushing ``main`` is refused by the git
  adapter); the platform never merges and never enables auto-merge;
* both side effects go through the effect ledger with probes (remote branch SHA,
  existing PR), so a crash and resume never pushes twice or opens two PRs (AC-04);
* network access to the remote and to the API is checked against the egress
  policy before anything leaves the machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from ai_coding.domain.enums import ApprovalOperation, TaskPhase
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.host import RepositoryHost
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.handlers import GitDiscoveryHandler
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult


@dataclass(frozen=True, slots=True)
class PullRequestRequest:
    """What to open."""

    branch: str
    base: str
    title: str
    body: str = ""
    draft: bool = False
    remote: str = "origin"


def _remote_host(url: str) -> str:
    if url.startswith(("https://", "ssh://", "http://")):
        return urlsplit(url).hostname or ""
    if "@" in url and ":" in url:  # scp-like git@host:path
        return url.split("@", 1)[1].split(":", 1)[0]
    return ""


class _Handler:
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True


class PushHandler(_Handler):
    """Pushes the work branch exactly once."""

    name = "builtin.git.push"
    capabilities = frozenset({"git.push"})

    def __init__(self, token: Callable[[], str | None]) -> None:
        self._token = token

    async def run(self, ctx: StepContext) -> StepResult:
        branch, remote = str(ctx.input["branch"]), str(ctx.input["remote"])
        repo = LocalGitRepository.open(
            ctx.project_root, protected_branches=ctx.config.project.protected_branches
        )
        repo.ensure_not_protected(branch)
        url = repo.remote_url(remote) or ""
        host = _remote_host(url)
        if host:
            ctx.authorize(ctx.policy.network(host))
        token = self._token()
        local_sha = repo.rev_parse(f"refs/heads/{branch}")

        def push() -> dict[str, str]:
            return {"sha": repo.push(branch, remote=remote, token=token)}

        def probe() -> dict[str, str] | None:
            remote_sha = repo.remote_branch_sha(branch, remote=remote, token=token)
            return {"sha": remote_sha} if remote_sha == local_sha else None

        result = await asyncio.to_thread(ctx.effect, "push", push, probe)
        return StepResult(output={"branch": branch, "sha": result["sha"], "remote": remote})


def _body(ctx: StepContext) -> str:
    """Static body, or ``pr_body`` produced by an upstream step (``body_from``)."""
    source = ctx.input.get("body_from")
    if source and source in ctx.dependencies:
        produced = ctx.dependencies[source].get("pr_body")
        if isinstance(produced, str) and produced.strip():
            return produced
    return str(ctx.input.get("body") or "")


class OpenPullRequestHandler(_Handler):
    """Opens (or reuses) the PR; never merges."""

    name = "builtin.github.pr"
    capabilities = frozenset({"github.pr"})

    def __init__(self, host: RepositoryHost, api_host: str) -> None:
        self._host = host
        self._api_host = api_host

    async def run(self, ctx: StepContext) -> StepResult:
        ctx.authorize(ctx.policy.network(self._api_host))
        ctx.authorize(ctx.policy.operation(ApprovalOperation.PR_CREATE))
        data = ctx.input

        def create() -> dict[str, object]:
            pr = self._host.create_pull_request(
                head=str(data["branch"]),
                base=str(data["base"]),
                title=str(data["title"]),
                body=_body(ctx),
                draft=bool(data["draft"]),
            )
            return {"number": pr.number, "url": pr.url, "created": pr.created}

        def probe() -> dict[str, object] | None:
            pr = self._host.find_pull_request(str(data["branch"]), str(data["base"]))
            return {"number": pr.number, "url": pr.url, "created": False} if pr else None

        result = await asyncio.to_thread(ctx.effect, "open-pr", create, probe)
        return StepResult(output=dict(result), evidence_refs=[str(result["url"])])


class VerifyPullRequestHandler(_Handler):
    """Checks that the PR exists, targets the base and points at the pushed commit."""

    name = "builtin.github.pr.verify"
    capabilities = frozenset({"github.pr.verify"})

    def __init__(self, host: RepositoryHost) -> None:
        self._host = host

    async def run(self, ctx: StepContext) -> StepResult:
        pushed = ctx.dependencies["git.push"]
        opened = ctx.dependencies["github.pr"]
        pr = await asyncio.to_thread(self._host.get_pull_request, int(opened["number"]))
        if pr.head_sha != pushed["sha"]:
            raise PermanentError("the PR head does not match the pushed commit")
        if pr.base != ctx.input["base"]:
            raise PermanentError("the PR targets an unexpected base branch")
        if pr.auto_merge:
            raise PermanentError("auto-merge is enabled on the PR; the platform forbids it")
        return StepResult(
            output={
                "number": opened["number"],
                "url": opened["url"],
                "head_sha": pushed["sha"],
                "verified": True,
            }
        )


def pull_request_registry(
    host: RepositoryHost,
    *,
    token: Callable[[], str | None],
    api_host: str = "api.github.com",
) -> HandlerRegistry:
    """Handlers of a PR run; the request itself lives in the persisted step inputs,
    so the same registry serves both the first execution and every resume."""
    return HandlerRegistry(
        [
            GitDiscoveryHandler(),
            PushHandler(token),
            OpenPullRequestHandler(host, api_host),
            VerifyPullRequestHandler(host),
        ]
    )


def build_pull_request(
    request: PullRequestRequest,
    host: RepositoryHost,
    *,
    token: Callable[[], str | None],
    api_host: str = "api.github.com",
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a PR run."""
    registry = pull_request_registry(host, token=token, api_host=api_host)
    data = {
        "branch": request.branch,
        "base": request.base,
        "title": request.title,
        "body": request.body,
        "draft": request.draft,
        "remote": request.remote,
    }
    steps = [
        StepSpec(key="discover.git", capability="discovery.git", phase=TaskPhase.DISCOVERY),
        StepSpec(
            key="git.push", capability="git.push", input=data, max_attempts=3, timeout_seconds=600
        ),
        StepSpec(
            key="github.pr",
            capability="github.pr",
            input=data,
            depends_on=("git.push",),
            requires_approval=ApprovalOperation.PR_CREATE,
            max_attempts=3,
        ),
        StepSpec(
            key="github.pr.verify",
            capability="github.pr.verify",
            input=data,
            phase=TaskPhase.VERIFICATION,
            depends_on=("git.push", "github.pr"),
        ),
    ]
    return registry, steps
