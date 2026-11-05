"""Orchestrator engine: FSM driving, DAG execution, retries, approvals, budget, cancel, leases."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from ai_coding.domain.enums import (
    ApprovalOperation,
    ProviderName,
    RunState,
    TaskPhase,
)
from ai_coding.llm import TokenUsage
from ai_coding.orchestrator import Orchestrator, RunOptions, StepContext, StepResult, TaskPlanner
from ai_coding.orchestrator.engine import short_id
from ai_coding.orchestrator.errors import (
    ApprovalDeniedError,
    ApprovalError,
    BudgetExceededError,
    IdempotencyConflictError,
    InvalidTransitionError,
    PermanentError,
    RunLeaseError,
    RunNotFoundError,
    TransientError,
)
from ai_coding.persistence.models import (
    AgentExecution,
    Checkpoint,
    ModelUsage,
    Project,
    ProjectMember,
    Run,
    Task,
    User,
)
from ai_coding.persistence.types import utcnow
from tests.helpers import handler, make_config, make_orchestrator, step

E, V, DSC = TaskPhase.EXECUTION, TaskPhase.VERIFICATION, TaskPhase.DISCOVERY


def plan(*steps):  # type: ignore[no-untyped-def]
    return TaskPlanner().build(list(steps))


async def run_plan(orch: Orchestrator, *steps, **kw):  # type: ignore[no-untyped-def]
    rid = orch.create_run(plan(*steps), **kw)
    return rid, await orch.execute(rid)


def states(orch: Orchestrator, rid: uuid.UUID) -> list[str]:
    return [e["to"] for e in orch.events(rid)]


async def test_full_lifecycle_and_journal(tmp_path: Path) -> None:
    h = handler("h", "cap")
    orch = make_orchestrator(tmp_path, [h])
    rid, status = await run_plan(
        orch,
        step("d", "cap", phase=DSC),
        step("e1", "cap", depends_on=("d",)),
        step("e2", "cap", depends_on=("d",)),
        step("v", "cap", phase=V, depends_on=("e1", "e2")),
    )
    assert status.state is RunState.COMPLETED
    assert status.summary.succeeded
    assert states(orch, rid) == [
        "CREATED", "DISCOVERY", "PLANNED", "RUNNING", "VERIFYING", "COMPLETED",
    ]  # fmt: skip
    assert h.calls.index("d") == 0 and h.calls[-1] == "v"
    with orch.db.session() as s:
        v = s.scalar(select(Task).where(Task.key == "v"))
        assert v is not None and v.output == {"key": "v", "deps": ["e1", "e2"]}
        labels = [c.label for c in s.scalars(select(Checkpoint).order_by(Checkpoint.seq))]
        assert labels[0] == "task:d" and "planned" in labels and labels[-1] == "final:COMPLETED"
        run = s.get(Run, rid)
        assert run is not None and run.lease_owner is None and run.finished_at is not None
    actions = {a["action"] for a in orch.audit_trail(rid)}
    assert {"run.create", "run.transition"} <= actions


async def test_parallelism_respects_limit(tmp_path: Path) -> None:
    async def slow(ctx: StepContext) -> StepResult:
        await asyncio.sleep(0.05)
        return StepResult()

    h = handler("h", "cap", behaviour=slow)
    orch = make_orchestrator(
        tmp_path, [h], config=make_config(execution__limits__max_parallel_tasks=3)
    )
    _, status = await run_plan(orch, *[step(f"s{i}", "cap") for i in range(8)])
    assert status.state is RunState.COMPLETED
    assert h.max_concurrent == 3


async def test_failure_skips_dependents_and_fails_run(tmp_path: Path) -> None:
    async def boom(ctx: StepContext) -> StepResult:
        raise PermanentError("compile error")

    orch = make_orchestrator(tmp_path, [handler("ok", "ok"), handler("bad", "bad", behaviour=boom)])
    _rid, status = await run_plan(
        orch,
        step("a", "bad"),
        step("b", "ok", depends_on=("a",)),
        step("c", "ok", depends_on=("b",)),
        step("independent", "ok"),
    )
    assert status.state is RunState.FAILED
    by_key = {t["key"]: t for t in status.tasks}
    assert by_key["a"]["status"] == "FAILED" and "compile error" in by_key["a"]["error"]
    assert by_key["a"]["attempts"] == 1  # permanent: no retry
    assert by_key["b"]["status"] == "SKIPPED" and by_key["c"]["status"] == "SKIPPED"
    assert by_key["independent"]["status"] == "SUCCEEDED"
    assert "task a" in (status.error or "")
    assert not status.summary.succeeded


async def test_transient_retry_then_success(tmp_path: Path) -> None:
    attempts: list[int] = []

    async def flaky(ctx: StepContext) -> StepResult:
        attempts.append(ctx.attempt)
        if ctx.attempt < 3:
            raise TransientError("network blip")
        return StepResult(output={"attempt": ctx.attempt})

    orch = make_orchestrator(tmp_path, [handler("f", "f", behaviour=flaky)])
    _rid, status = await run_plan(orch, step("x", "f", max_attempts=3))
    assert status.state is RunState.COMPLETED
    assert attempts == [1, 2, 3]
    with orch.db.session() as s:
        execs = list(s.scalars(select(AgentExecution).order_by(AgentExecution.attempt)))
        assert [e.status for e in execs] == ["failed", "failed", "succeeded"]


async def test_retries_exhausted(tmp_path: Path) -> None:
    async def flaky(ctx: StepContext) -> StepResult:
        raise TransientError("still down")

    orch = make_orchestrator(tmp_path, [handler("f", "f", behaviour=flaky)])
    _, status = await run_plan(orch, step("x", "f", max_attempts=2))
    assert status.state is RunState.FAILED
    assert status.tasks[0]["attempts"] == 2


async def test_timeout_is_retried_then_fails(tmp_path: Path) -> None:
    async def hang(ctx: StepContext) -> StepResult:
        await asyncio.sleep(10)
        return StepResult()  # pragma: no cover

    orch = make_orchestrator(tmp_path, [handler("h", "h", behaviour=hang)])
    _, status = await run_plan(orch, step("x", "h", timeout_seconds=0.05, max_attempts=2))
    assert status.state is RunState.FAILED
    assert status.tasks[0]["attempts"] == 2
    assert "TimeoutError" in status.tasks[0]["error"]


async def test_success_criteria_enforced(tmp_path: Path) -> None:
    """A handler claiming success with confidence 1.0 still fails if criteria are not met."""

    async def lying(ctx: StepContext) -> StepResult:
        return StepResult(output={"tests_passed": False}, confidence=1.0)

    orch = make_orchestrator(tmp_path, [handler("h", "h", behaviour=lying)])
    criteria = {"type": "object", "properties": {"tests_passed": {"const": True}}}
    _, status = await run_plan(orch, step("t", "h", success_criteria=criteria, max_attempts=3))
    assert status.state is RunState.FAILED
    assert status.tasks[0]["attempts"] == 1  # permanent
    assert "success criteria" in status.tasks[0]["error"]
    assert not status.summary.succeeded


async def test_handler_must_return_step_result(tmp_path: Path) -> None:
    async def wrong(ctx: StepContext) -> StepResult:
        return {"x": 1}  # type: ignore[return-value]

    orch = make_orchestrator(tmp_path, [handler("h", "h", behaviour=wrong)])
    _, status = await run_plan(orch, step("t", "h"))
    assert status.state is RunState.FAILED


async def test_no_route_fails_run(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [])
    _, status = await run_plan(orch, step("t", "missing"))
    assert status.state is RunState.FAILED
    assert "no handler for capability 'missing'" in status.tasks[0]["error"]


async def test_discovery_failure_fails_run_before_planning(tmp_path: Path) -> None:
    async def boom(ctx: StepContext) -> StepResult:
        raise PermanentError("no access")

    orch = make_orchestrator(tmp_path, [handler("b", "b", behaviour=boom), handler("ok", "ok")])
    rid, status = await run_plan(orch, step("d", "b", phase=DSC), step("e", "ok"))
    assert status.state is RunState.FAILED
    assert states(orch, rid) == ["CREATED", "DISCOVERY", "FAILED"]
    assert {t["key"]: t["status"] for t in status.tasks}["e"] == "PENDING"


# ----------------------------------------------------------------- approvals --
async def test_plan_approval_flow(tmp_path: Path) -> None:
    h = handler("h", "cap")
    orch = make_orchestrator(tmp_path, [h])
    rid, status = await run_plan(
        orch, step("migrate", "cap", requires_approval=ApprovalOperation.MIGRATION)
    )
    assert status.state is RunState.AWAITING_APPROVAL
    assert h.calls == []
    assert [a["operation"] for a in status.approvals] == ["migration"]
    resumed = await orch.resume(rid)  # still pending: stays waiting
    assert resumed.state is RunState.AWAITING_APPROVAL
    decided = orch.decide(rid, approve=True, note="reviewed")
    assert [a.status for a in decided] == ["approved"]
    final = await orch.resume(rid)
    assert final.state is RunState.COMPLETED
    assert h.calls == ["migrate"]
    actions = [a["action"] for a in orch.audit_trail(rid)]
    assert (
        "approval.request" in actions and "approval.approve" in actions and "run.resume" in actions
    )


async def test_plan_approval_rejected_cancels(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    rid, _ = await run_plan(orch, step("deploy", "cap", requires_approval=ApprovalOperation.DEPLOY))
    orch.decide(rid, approve=False, note="not today")
    final = await orch.resume(rid)
    assert final.state is RunState.CANCELLED
    assert final.tasks[0]["status"] == "CANCELLED"


async def test_require_approval_option(tmp_path: Path) -> None:
    config = make_config(approvals__required_for=sorted(ApprovalOperation.mandatory()))
    orch = make_orchestrator(tmp_path, [handler("h", "cap")], config=config)
    s = step("pr", "cap", requires_approval=ApprovalOperation.PR_CREATE)
    _, relaxed = await run_plan(orch, s)
    assert relaxed.state is RunState.COMPLETED
    _, strict = await run_plan(orch, s, options=RunOptions(require_approval=True))
    assert strict.state is RunState.AWAITING_APPROVAL


async def test_mid_run_approval_via_policy(tmp_path: Path) -> None:
    async def writes_ci(ctx: StepContext) -> StepResult:
        ctx.authorize(ctx.policy.file_write(".github/workflows/ci.yml"))
        return StepResult(output={"written": True})

    orch = make_orchestrator(tmp_path, [handler("w", "w", behaviour=writes_ci)])
    rid, status = await run_plan(orch, step("edit", "w"))
    assert status.state is RunState.AWAITING_APPROVAL
    assert status.approvals[0]["operation"] == "critical_file_change"
    assert status.tasks[0]["status"] == "PENDING"
    orch.decide(rid, approve=True)
    final = await orch.resume(rid)
    assert final.state is RunState.COMPLETED
    assert final.tasks[0]["attempts"] == 2


async def test_policy_denial_fails_task(tmp_path: Path) -> None:
    async def escapes(ctx: StepContext) -> StepResult:
        ctx.authorize(ctx.policy.file_write("../outside.txt"))
        return StepResult()  # pragma: no cover

    orch = make_orchestrator(tmp_path, [handler("w", "w", behaviour=escapes)])
    _, status = await run_plan(orch, step("edit", "w", max_attempts=3))
    assert status.state is RunState.FAILED
    assert "PolicyDeniedError" in status.tasks[0]["error"]
    assert status.tasks[0]["attempts"] == 1


def test_decide_rbac(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    rid = orch.create_run(plan(step("m", "cap", requires_approval=ApprovalOperation.MERGE)))
    asyncio.run(orch.execute(rid))
    with pytest.raises(ApprovalDeniedError, match="not a member"):
        orch.decide(rid, approve=True, actor="local:stranger")
    with orch.db.session() as s:
        project = s.scalar(select(Project))
        assert project is not None
        dev = User(username="local:dev")
        s.add(dev)
        s.flush()
        s.add(ProjectMember(project_id=project.id, user_id=dev.id, role="developer"))
    with pytest.raises(ApprovalDeniedError, match="cannot decide"):
        orch.decide(rid, approve=True, actor="local:dev")
    denied = [a for a in orch.audit_trail(rid) if a["outcome"] == "denied"]
    assert {d["actor"] for d in denied} == {"local:stranger", "local:dev"}
    with pytest.raises(ApprovalError, match="no pending approval"):
        orch.decide(rid, approve=True, approval_id=uuid.uuid4())


# ---------------------------------------------------------------- budget --
async def test_budget_exhaustion_pauses_and_usage_persists(tmp_path: Path) -> None:
    async def spend(ctx: StepContext) -> StepResult:
        ctx.budget.check(extra_cost_usd=0.6)
        ctx.record_usage(ProviderName.OLLAMA, "m", TokenUsage(100, 50), 0.6)
        ctx.budget.add(tokens=150, cost_usd=0.6)
        return StepResult()

    config = make_config(budgets__max_cost_usd_per_run=1.0)
    orch = make_orchestrator(tmp_path, [handler("s", "s", behaviour=spend)], config=config)
    rid, status = await run_plan(orch, step("a", "s"), step("b", "s", depends_on=("a",)))
    assert status.state is RunState.PAUSED
    assert {t["key"]: t["status"] for t in status.tasks} == {"a": "SUCCEEDED", "b": "PENDING"}
    with orch.db.session() as s:
        assert s.query(ModelUsage).count() == 1
    # Same budget: the persisted usage is restored, so resuming pauses again.
    again = await orch.resume(rid)
    assert again.state is RunState.PAUSED
    # Raised budget: the run continues from its checkpoint; 'a' is not re-executed.
    bigger = make_orchestrator(
        tmp_path, [handler("s", "s", behaviour=spend)], db=orch.db,
        config=make_config(budgets__max_cost_usd_per_run=5.0),
    )  # fmt: skip
    final = await bigger.resume(rid)
    assert final.state is RunState.COMPLETED
    # 'a' ran once; 'b' ran each time it hit the budget inside the handler.
    assert {t["key"]: t["attempts"] for t in final.tasks} == {"a": 1, "b": 3}
    assert "PAUSED" in states(orch, rid)


async def test_budget_error_from_handler_pauses(tmp_path: Path) -> None:
    async def over(ctx: StepContext) -> StepResult:
        raise BudgetExceededError("token budget exceeded")

    orch = make_orchestrator(tmp_path, [handler("o", "o", behaviour=over)])
    _, status = await run_plan(orch, step("a", "o"))
    assert status.state is RunState.PAUSED


# ---------------------------------------------------------------- cancel --
async def test_cancel_while_running_is_cooperative(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def long(ctx: StepContext) -> StepResult:
        started.set()
        await asyncio.sleep(10)
        return StepResult()  # pragma: no cover

    worker = make_orchestrator(tmp_path, [handler("l", "l", behaviour=long)])
    other = make_orchestrator(tmp_path, [], db=worker.db, actor="local:tester")
    rid = worker.create_run(plan(step("a", "l"), step("b", "l", depends_on=("a",))))
    execution = asyncio.create_task(worker.execute(rid))
    await asyncio.wait_for(started.wait(), 5)
    requested = other.cancel(rid, reason="user abort")
    assert requested.state is RunState.RUNNING  # a live worker holds the lease
    final = await asyncio.wait_for(execution, 5)
    assert final.state is RunState.CANCELLED
    assert {t["status"] for t in final.tasks} == {"CANCELLED"}


async def test_cancel_idle_run_and_terminal_errors(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    rid, status = await run_plan(orch, step("m", "cap", requires_approval=ApprovalOperation.MERGE))
    assert status.state is RunState.AWAITING_APPROVAL
    cancelled = orch.cancel(rid)
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.approvals[0]["status"] == "expired"
    with pytest.raises(InvalidTransitionError):
        orch.cancel(rid)
    with pytest.raises(InvalidTransitionError):
        await orch.resume(rid)


async def test_cancel_before_start(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    rid = orch.create_run(plan(step("a", "cap")))
    orch.cancel(rid)
    assert states(orch, rid) == ["CREATED", "CANCELLED"]


# ----------------------------------------------------------- idempotency --
def test_create_run_is_idempotent(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    p = plan(step("a", "cap"))
    first = orch.create_run(p, idempotency_key="req-1")
    assert orch.create_run(p, idempotency_key="req-1") == first
    with pytest.raises(IdempotencyConflictError):
        orch.create_run(plan(step("b", "cap")), idempotency_key="req-1")
    assert orch.create_run(p) != first
    assert len(orch.list_runs()) == 2


# ---------------------------------------------------------------- leases --
async def test_lease_prevents_concurrent_workers(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")], lease_ttl=60)
    rid = orch.create_run(plan(step("a", "cap")))
    with orch.db.session() as s:
        run = s.get(Run, rid)
        assert run is not None
        run.lease_owner = "other-host:123"
        run.lease_expires_at = utcnow() + timedelta(minutes=5)
    with pytest.raises(RunLeaseError, match="leased by other-host"):
        await orch.execute(rid)
    with pytest.raises(RunLeaseError):
        await orch.resume(rid)
    final = await orch.resume(rid, force=True)
    assert final.state is RunState.COMPLETED


async def test_expired_lease_can_be_taken_over(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    rid = orch.create_run(plan(step("a", "cap")))
    with orch.db.session() as s:
        run = s.get(Run, rid)
        assert run is not None
        run.lease_owner = "dead-worker"
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
    assert (await orch.resume(rid)).state is RunState.COMPLETED


async def test_lost_lease_stops_worker(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def long(ctx: StepContext) -> StepResult:
        started.set()
        await asyncio.sleep(10)
        return StepResult()  # pragma: no cover

    orch = make_orchestrator(tmp_path, [handler("l", "l", behaviour=long)], lease_ttl=0.15)
    rid = orch.create_run(plan(step("a", "l")))
    execution = asyncio.create_task(orch.execute(rid))
    await asyncio.wait_for(started.wait(), 5)
    with orch.db.session() as s:
        run = s.get(Run, rid)
        assert run is not None
        run.lease_owner = "thief"
    status = await asyncio.wait_for(execution, 5)
    assert status.state is RunState.RUNNING  # the worker stopped without transitions
    assert status.lease_owner == "thief"


# ---------------------------------------------------------------- queries --
def test_preview_writes_nothing(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    previews = orch.preview(
        plan(step("a", "cap"), step("m", "cap", requires_approval=ApprovalOperation.MERGE),
             step("x", "missing")),
        RunOptions(require_approval=True),
    )  # fmt: skip
    by_key = {p.key: p for p in previews}
    assert by_key["a"].handler == "h" and by_key["a"].approval is None
    assert (
        by_key["m"].approval is not None
        and by_key["m"].approval.verdict.value == "require_approval"
    )
    assert by_key["x"].handler is None and "no handler" in (by_key["x"].route_error or "")
    with orch.db.session() as s:
        assert s.query(Run).count() == 0


def test_resolve_run_id(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    a = orch.create_run(plan(step("a", "cap")))
    b = orch.create_run(plan(step("b", "cap")))
    assert orch.resolve_run_id(short_id(a)) == a
    assert orch.resolve_run_id(str(b)) == b
    with pytest.raises(RunNotFoundError, match="ambiguous"):
        orch.resolve_run_id(str(a)[:4])
    with pytest.raises(RunNotFoundError, match="no run"):
        orch.resolve_run_id("ffffffffffff")
    with pytest.raises(RunNotFoundError, match="empty"):
        orch.resolve_run_id(" ")
    with pytest.raises(RunNotFoundError):
        orch.status(uuid.uuid4())


async def test_effects_and_artifacts_from_handlers(tmp_path: Path) -> None:
    performed: list[str] = []

    async def effectful(ctx: StepContext) -> StepResult:
        result = ctx.effect("publish", lambda: performed.append(ctx.task_key) or {"id": 1})
        ref = ctx.save_artifact("report.txt", b"token=ghp_" + b"a" * 36, "text/plain")
        return StepResult(output={"published": result["id"]}, evidence_refs=[ref.uri])

    orch = make_orchestrator(tmp_path, [handler("e", "e", behaviour=effectful)])
    _rid, status = await run_plan(orch, step("p", "e"))
    assert status.state is RunState.COMPLETED and performed == ["p"]
    artifact = status.summary.artifacts[0]
    data = orch.artifacts.get(artifact["sha256"])
    assert b"ghp_" not in data  # artifacts are redacted (ТЗ §12)


def test_project_and_owner_created_once(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, [handler("h", "cap")])
    orch.create_run(plan(step("a", "cap")))
    orch.create_run(plan(step("a", "cap")))
    with orch.db.session() as s:
        assert s.query(Project).count() == 1
        member = s.query(ProjectMember).one()
        assert member.role == "owner"
        project = s.query(Project).one()
        assert orch.role_of(s, "local:tester", project.id) is not None
        assert orch.role_of(s, "nobody", project.id) is None
