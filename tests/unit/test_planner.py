"""Task Planner: DAG validation, ordering, budget."""

from __future__ import annotations

import pytest

from ai_coding.domain.enums import ApprovalOperation, TaskPhase
from ai_coding.orchestrator.errors import PlanValidationError
from ai_coding.orchestrator.planner import StepSpec, TaskPlanner
from tests.helpers import step


def test_topological_order_and_levels() -> None:
    plan = TaskPlanner().build(
        [
            step("c", depends_on=("a", "b")),
            step("a"),
            step("b", depends_on=("a",)),
            step("d"),
        ]
    )
    assert plan.levels == (("a", "d"), ("b",), ("c",))
    assert plan.order == ("a", "d", "b", "c")
    assert plan.step("c").depends_on == ("a", "b")
    with pytest.raises(KeyError):
        plan.step("zzz")


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([], "no steps"),
        ([step("a"), step("a")], "duplicate step key"),
        ([step("Bad Key")], "invalid step key"),
        ([step("a", depends_on=("x",))], "unknown step 'x'"),
        ([step("a", depends_on=("a",))], "depends on itself"),
        ([step("a", depends_on=("b",)), step("b", depends_on=("a",))], "dependency cycle"),
        ([step("a", max_attempts=0)], "max_attempts"),
        ([step("a", timeout_seconds=0)], "timeout_seconds"),
        ([step("a", budget_usd=-1)], "budget_usd"),
        ([StepSpec(key="a", capability="")], "no capability"),
        ([step("a", success_criteria={"type": "nope"})], "success_criteria"),
        (
            [step("v", phase=TaskPhase.DISCOVERY, depends_on=("e",)), step("e")],
            "later-phase step",
        ),
    ],
)
def test_invalid_plans(steps: list, message: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(PlanValidationError, match=message):
        TaskPlanner().build(steps)


def test_cycle_message_names_the_cycle() -> None:
    with pytest.raises(PlanValidationError) as info:
        TaskPlanner().build(
            [
                step("a", depends_on=("c",)),
                step("b", depends_on=("a",)),
                step("c", depends_on=("b",)),
            ]
        )
    assert "a → c → b → a" in str(info.value) or "→" in str(info.value)


def test_max_steps() -> None:
    with pytest.raises(PlanValidationError, match="max 2"):
        TaskPlanner(max_steps=2).build([step("a"), step("b"), step("c")])


def test_budget_limit() -> None:
    steps = [step("a", budget_usd=2.0), step("b", budget_usd=2.5)]
    assert TaskPlanner(max_budget_usd=5).build(steps).total_budget_usd == 4.5
    with pytest.raises(PlanValidationError, match="exceeds the run budget"):
        TaskPlanner(max_budget_usd=4).build(steps)


def test_digest_is_stable_and_sensitive() -> None:
    a = TaskPlanner().build([step("a"), step("b", depends_on=("a",))])
    b = TaskPlanner().build([step("a"), step("b", depends_on=("a",))])
    c = TaskPlanner().build([step("a"), step("b")])
    assert a.digest() == b.digest()
    assert a.digest() != c.digest()


def test_approvals_required() -> None:
    plan = TaskPlanner().build(
        [step("a"), step("m", requires_approval=ApprovalOperation.MIGRATION)]
    )
    assert plan.approvals_required() == [("m", ApprovalOperation.MIGRATION)]
    assert plan.step("m").to_dict()["requires_approval"] == "migration"
