"""MAINTAIN (ТЗ §3, B-610): triage → approved fix plan → one small verified PR per item.

DAG::

    discover.git → maintain.baseline → maintain.plan → maintain.approve
                 → [maintain.deps] → maintain.item → maintain.verify → [git.push → github.pr …]

* sources are issues (GitHub or a file), findings and vulnerable dependencies from an
  audit report, and optional telemetry; all are untrusted data for the agent;
* ``maintain.plan`` — the Maintenance Agent plans or defers every source (checked:
  nothing silently dropped, upgrades only to scanner-reported fixed versions, each
  item names its regression test); ``maintain.approve`` — a human approves the exact
  plan version (plan-level approval, like MODERNIZE);
* ``maintain.deps`` — dependency upgrades are applied by the ecosystem's package
  manager in the sandbox (real lock files, policy-gated registry egress);
* ``maintain.item`` — the S6 implement loop starts from those changes; the Developer
  adds the regression test, the tests must pass (strict when the baseline is green);
* ``maintain.verify`` re-checks the committed content and tests; the PR never merges.

Later items of an approved plan run with ``--continue RUN [--item ID]``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from ai_coding.agents import MaintenanceAgent
from ai_coding.domain.enums import TaskPhase
from ai_coding.executors import DockerSandboxExecutor
from ai_coding.executors.base import Executor
from ai_coding.git import LocalGitRepository
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.orchestrator.handlers import GitDiscoveryHandler
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult
from ai_coding.tools.deps import DependencyUpdateError, Upgrade, apply_upgrades, ecosystem_of
from ai_coding.tools.testrun import TestStatus
from ai_coding.workflows.agent_runtime import run_agent
from ai_coding.workflows.develop import ImplementHandler, VerifyChangeHandler, branch_for
from ai_coding.workflows.implement import ImplementSettings
from ai_coding.workflows.modernize import PR, BaselineHandler, PRSpec, with_pull_request

DEPENDENCY_TOOLS = frozenset({"trivy", "osv-scanner"})
MAX_FINDINGS = 100
MAX_ISSUES = 50
_PRIORITY = ("p0", "p1", "p2", "p3")
_SEVERITY = ("critical", "high", "medium", "low", "info")


# ------------------------------------------------------------------ sources --
def _fixed_versions(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(v).strip() for v in items if str(v).strip()]


def sources_from_report(report: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:  # type: ignore[type-arg]
    """``(findings, dependencies)`` of an audit report (new and expired-baseline only).

    Vulnerabilities of the same package/version/lock file are grouped into one
    dependency with the union of the fixed versions the scanners reported.
    """
    open_findings = [f for f in report.get("findings", []) if f.get("status") != "baselined"]
    deps: dict[tuple[str, str, str], dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for f in sorted(open_findings, key=lambda f: (_SEVERITY.index(f["severity"]),
                                                   f.get("path") or "", f["rule_id"])):  # fmt: skip
        ev = f.get("evidence") or {}
        if f["tool"] in DEPENDENCY_TOOLS and ev.get("package"):
            path = str(f.get("path") or "")
            key = (path, str(ev["package"]), str(ev.get("installed") or ""))
            dep = deps.setdefault(key, {
                "package": key[1], "version": key[2], "path": path,
                "ecosystem": ev.get("ecosystem") or ecosystem_of(path) or "unknown",
                "fixed_versions": [], "advisories": [], "severity": f["severity"],
            })  # fmt: skip
            dep["advisories"] = sorted({*dep["advisories"], f["rule_id"]})
            dep["fixed_versions"] = sorted({*dep["fixed_versions"],
                                            *_fixed_versions(ev.get("fixed"))})  # fmt: skip
        elif len(findings) < MAX_FINDINGS:
            findings.append({"id": f["fingerprint"][:12], "tool": f["tool"],
                             "rule_id": f["rule_id"], "severity": f["severity"],
                             "path": f.get("path"), "line": f.get("line"),
                             "title": f["title"]})  # fmt: skip
    dependencies = [{"id": f"D{n}", **d} for n, d in enumerate(deps.values(), 1)]
    return findings, dependencies


@dataclass(frozen=True, slots=True)
class MaintainRequest:
    """What to maintain."""

    issues: tuple[Mapping[str, Any], ...] = ()
    findings: tuple[Mapping[str, Any], ...] = ()
    dependencies: tuple[Mapping[str, Any], ...] = ()
    telemetry: str | None = None
    base: str = "main"
    item: str | None = None
    plan: Mapping[str, Any] | None = None  # an approved plan (continue with the next item)
    plan_version: str | None = None
    sources: Mapping[str, Any] | None = None  # the sources the approved plan was made from
    allow_red_baseline: bool = False
    settings: ImplementSettings = field(default_factory=ImplementSettings)

    def source_payload(self) -> dict[str, Any]:
        """Inputs of the Maintenance Agent."""
        if self.sources is not None:
            return dict(self.sources)
        return {"issues": [dict(i) for i in self.issues][:MAX_ISSUES],
                "findings": [dict(f) for f in self.findings][:MAX_FINDINGS],
                "dependencies": [dict(d) for d in self.dependencies],
                "telemetry": self.telemetry}  # fmt: skip


def plan_version(plan: Mapping[str, Any]) -> str:
    """Content-addressed plan version (``m-<sha8>``): approval binds to the exact plan."""
    return "m-" + hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:8]


def pick_item(plan: Mapping[str, Any], item_id: str | None) -> dict[str, Any]:
    """Item ``item_id``, or the highest-priority one (plan order breaks ties)."""
    items = list(plan.get("items", []))
    if not items:
        raise PermanentError("the maintenance plan has no items (everything was deferred)")
    if item_id is None:
        return dict(min(items, key=lambda i: _PRIORITY.index(i["priority"])))
    for item in items:
        if item["id"] == item_id:
            return dict(item)
    raise PermanentError(f"item {item_id} is not in the plan ({', '.join(i['id'] for i in items)})")


def next_item_id(plan: Mapping[str, Any], done: Sequence[str]) -> str | None:
    """Highest-priority item not yet done."""
    rest = [i for i in plan.get("items", []) if i["id"] not in set(done)]
    return min(rest, key=lambda i: _PRIORITY.index(i["priority"]))["id"] if rest else None


def render_plan(plan: Mapping[str, Any], version: str) -> str:
    """Markdown of a maintenance plan (artifact and approval text)."""
    lines = [f"# Maintenance plan {version}", "", plan["summary"], "", "## Items", ""]
    for i in sorted(plan["items"], key=lambda i: _PRIORITY.index(i["priority"])):
        lines += [f"### {i['id']} [{i['priority']}, {i['kind']}, risk {i['risk']}] {i['title']}",
                  "", i["plan"], "", f"Sources: {', '.join(i['sources'])}",
                  f"Files: {', '.join(i['files']) or '-'}",
                  f"Regression test: `{i['regression_test']['file']}` — "
                  f"{i['regression_test']['description']}"]  # fmt: skip
        lines += [f"Upgrade: {u['source']} → {u['to_version']}" for u in i.get("upgrades", [])]
        lines.append("")
    if plan.get("deferred"):
        lines += ["## Deferred", ""] + [f"- {d['source']}: {d['reason']}" for d in plan["deferred"]]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- handlers --
class MaintenancePlanHandler:
    """``maintain.plan``: triage of the sources into a plan (Maintenance Agent)."""

    name = "agent.maintenance"
    capabilities = frozenset({"agent.maintenance"})
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
        sources = dict(ctx.input["sources"])
        baseline = ctx.dependencies["maintain.baseline"]
        files = LocalGitRepository.open(ctx.project_root).list_files(baseline["commit"])
        agent_ctx, result = await run_agent(
            ctx, MaintenanceAgent(), {**sources, "files": files[:3000]},
            transport=self._transport, env=self._env,
        )  # fmt: skip
        plan = result.output
        version = plan_version(plan)
        refs = [
            ctx.save_artifact("maintenance-plan.json",
                              json.dumps(plan, indent=2, ensure_ascii=False).encode(),
                              "application/json").uri,
            ctx.save_artifact("maintenance-plan.md", render_plan(plan, version).encode(),
                              "text/markdown").uri,
        ]  # fmt: skip
        return StepResult(
            output={"plan": plan, "version": version, "sources": sources,
                    "models": list(dict.fromkeys(agent_ctx.models))},
            evidence_refs=refs + list(result.evidence_refs), confidence=result.confidence,
        )  # fmt: skip


class MaintenanceApprovalHandler:
    """``maintain.approve``: a human approves the exact plan version."""

    name = "builtin.maintain.approve"
    capabilities = frozenset({"maintain.approve"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        version = str(ctx.dependencies["maintain.plan"]["version"])
        ctx.authorize(ctx.policy.plan_approval(version))
        return StepResult(output={"approved_version": version})


def _plan_of(ctx: StepContext) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if "maintain.plan" in ctx.dependencies:
        out = ctx.dependencies["maintain.plan"]
        return dict(out["plan"]), str(out["version"]), dict(out["sources"])
    return dict(ctx.input["plan"]), str(ctx.input["plan_version"]), dict(ctx.input["sources"])


def upgrades_of(item: Mapping[str, Any], sources: Mapping[str, Any]) -> list[Upgrade]:
    """Deterministic upgrades of a plan item (only to scanner-reported fixed versions)."""
    deps = {str(d["id"]): d for d in sources.get("dependencies") or []}
    result: list[Upgrade] = []
    for u in item.get("upgrades", []):
        dep = deps.get(u["source"])
        if dep is None or u["to_version"] not in (dep.get("fixed_versions") or []):
            raise PermanentError(f"{item['id']}: upgrade {u['source']} → {u['to_version']} "
                                 "is not backed by a scanner-reported fix")  # fmt: skip
        directory = Path(str(dep.get("path") or ".")).parent.as_posix()
        result.append(Upgrade(str(dep["ecosystem"]), str(dep["package"]), u["to_version"],
                              directory or "."))  # fmt: skip
    return result


class DependencyUpgradeHandler:
    """``maintain.deps``: the package manager applies the item's upgrades (sandbox)."""

    name = "builtin.maintain.deps"
    capabilities = frozenset({"maintain.deps"})
    cost_usd = 0.0

    def __init__(self, *, executor: Executor | None = None) -> None:
        self._executor = executor

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        plan, _, sources = _plan_of(ctx)
        item = pick_item(plan, ctx.input.get("item"))
        upgrades = upgrades_of(item, sources)
        if not upgrades:
            return StepResult(output={"changes": [], "upgrades": []})
        repo = LocalGitRepository.open(ctx.project_root)
        commit = str(ctx.dependencies["maintain.baseline"]["commit"])
        docker = ctx.config.execution.docker
        executor = self._executor or DockerSandboxExecutor(
            limits=ctx.config.execution.limits, docker_config=docker
        )
        with tempfile.TemporaryDirectory(prefix="ai-coding-maintain-") as tmp:
            tree = Path(tmp) / "tree"
            repo.export(commit, tree)
            try:
                changes = await apply_upgrades(
                    executor, tree, upgrades,
                    network_allowed=lambda host: ctx.policy.network(host).allowed,
                    sandbox_network=docker.network == "bridge",
                )  # fmt: skip
            except DependencyUpdateError as exc:
                raise PermanentError(f"{item['id']}: {exc}") from exc
        return StepResult(output={
            "changes": changes,
            "upgrades": [{"ecosystem": u.ecosystem, "package": u.package,
                          "version": u.version, "directory": u.directory} for u in upgrades],
        })  # fmt: skip


class MaintenanceItemHandler(ImplementHandler):
    """``maintain.item``: one plan item through the implement loop, then a commit."""

    name = "builtin.maintain.item"
    capabilities = frozenset({"maintain.item"})

    def prepare(self, ctx: StepContext) -> tuple[dict[str, Any], list[str], ImplementSettings]:
        plan, _, _ = _plan_of(ctx)
        item = pick_item(plan, ctx.input.get("item"))
        test = item["regression_test"]
        upgraded = ctx.dependencies.get("maintain.deps", {}).get("upgrades") or []
        body = (
            f"Maintenance item {item['id']} ({item['kind']}, {item['priority']}): {item['plan']}"
            f"\n\nFiles: {', '.join(item['files']) or '-'}\n"
            f"Regression test (required): add or update `{test['file']}` — "
            f"{test['description']}. It must fail without the fix and pass with it.\n"
        )
        if upgraded:
            body += ("\nThe dependency upgrades are already applied by the package manager "
                     "(manifest and lock file); do not edit lock files. Adapt the code if the "
                     "new version needs it: "
                     + ", ".join(f"{u['package']} {u['version']}" for u in upgraded))  # fmt: skip
        settings = ImplementSettings(**ctx.input["settings"])
        settings = replace(settings, require_tests=True)
        if ctx.dependencies["maintain.baseline"]["tests"]["status"] == TestStatus.PASSED.value:
            settings = replace(settings, strict=True)  # green baseline: no regressions
        hints = [*item["files"], test["file"]]
        return {"title": f"{item['id']}: {item['title']}", "body": body}, hints, settings

    def seed_changes(self, ctx: StepContext) -> list[dict[str, Any]]:
        return list(ctx.dependencies.get("maintain.deps", {}).get("changes") or [])

    def extra_output(self, ctx: StepContext) -> dict[str, Any]:
        plan, version, sources = _plan_of(ctx)
        item = pick_item(plan, ctx.input.get("item"))
        done = [*ctx.input.get("done", ()), item["id"]]
        return {"item": item, "maintenance_plan": plan, "plan_version": version,
                "sources": sources, "done": done,
                "next_item": next_item_id(plan, done)}  # fmt: skip


def build_maintain(
    request: MaintainRequest,
    *,
    pull_request: PR | None = None,
    executor: Executor | None = None,
    deps_executor: Executor | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
    done: Sequence[str] = (),
) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and DAG of a MAINTAIN run."""
    handlers: list[Any] = [
        GitDiscoveryHandler(),
        BaselineHandler(executor=executor),
        DependencyUpgradeHandler(executor=deps_executor),
        MaintenanceItemHandler(executor=executor, transport=transport, env=env),
        VerifyChangeHandler(executor=executor, source="maintain.item",
                            name="builtin.maintain.verify", capability="maintain.verify"),
    ]  # fmt: skip
    steps = [
        StepSpec(key="discover.git", capability="discovery.git", phase=TaskPhase.DISCOVERY),
        StepSpec(key="maintain.baseline", capability="modernize.baseline",
                 depends_on=("discover.git",), timeout_seconds=1800,
                 input={"base": request.base, "allow_red_baseline": request.allow_red_baseline}),
    ]  # fmt: skip
    item_input: dict[str, Any] = {
        "base": request.base,
        "item": request.item,
        "done": list(done),
        "settings": {
            "max_iterations": request.settings.max_iterations,
            "require_tests": True,
            "strict": request.settings.strict,
            "review": request.settings.review,
        },
    }
    deps: tuple[str, ...] = ("maintain.baseline",)
    if request.plan is None:
        handlers += [MaintenancePlanHandler(transport=transport, env=env),
                     MaintenanceApprovalHandler()]  # fmt: skip
        steps += [
            StepSpec(key="maintain.plan", capability="agent.maintenance",
                     depends_on=("maintain.baseline",), max_attempts=3,
                     input={"sources": request.source_payload()}),
            StepSpec(key="maintain.approve", capability="maintain.approve",
                     depends_on=("maintain.plan",)),
        ]  # fmt: skip
        deps = ("maintain.baseline", "maintain.plan", "maintain.approve")
        title = "maintenance"
    else:
        item_input |= {"plan": dict(request.plan), "plan_version": request.plan_version,
                       "sources": request.source_payload()}  # fmt: skip
        title = pick_item(request.plan, request.item)["title"]
    suffix = uuid.uuid4().hex[:6]  # keeps runs apart (re-planning, retries of an item)
    if request.plan is None:  # the item is chosen only after the plan is approved
        item_id, branch = "next", f"ai/maintain/plan-{suffix}"
    else:
        item_id = request.item or pick_item(request.plan, None)["id"]
        branch = f"{branch_for(f'{item_id} {title}', prefix='ai/maintain')}-{suffix}"
    item_input |= {"branch": branch, "commit_message": f"fix: maintenance {item_id}"[:72]}
    steps += [
        StepSpec(key="maintain.deps", capability="maintain.deps", depends_on=deps,
                 input={k: v for k, v in item_input.items() if k != "settings"},
                 max_attempts=2, timeout_seconds=1800),
        StepSpec(key="maintain.item", capability="maintain.item",
                 depends_on=(*deps, "maintain.deps"), input=item_input, max_attempts=2,
                 timeout_seconds=3600),
        StepSpec(key="maintain.verify", capability="maintain.verify",
                 depends_on=("maintain.item",), timeout_seconds=1800),
    ]  # fmt: skip
    if pull_request is not None:
        with_pull_request(handlers, steps,
                          PRSpec(branch, request.base, f"Maintenance {item_id}: {title}",
                                 "maintain.item", "maintain.verify"), pull_request)  # fmt: skip
    return HandlerRegistry(handlers), steps
