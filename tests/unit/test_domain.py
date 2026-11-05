"""Tests for domain enumerations."""

from ai_coding.domain import (
    AgentName,
    ApprovalOperation,
    GateResult,
    Mode,
    ProviderName,
    RunState,
    Severity,
)


def test_modes_match_tz() -> None:
    assert [m.name for m in Mode] == ["CREATE", "DEVELOP", "MODERNIZE", "AUDIT", "MAINTAIN"]


def test_run_states_match_tz() -> None:
    assert {s.value for s in RunState} == {
        "CREATED", "DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING",
        "VERIFYING", "COMPLETED", "FAILED", "CANCELLED", "PAUSED",
    }  # fmt: skip


def test_twelve_agents() -> None:
    assert len(AgentName) == 12


def test_gate_results() -> None:
    assert [g.value for g in GateResult] == ["PASS", "FAIL", "SKIPPED"]


def test_provider_locality() -> None:
    assert ProviderName.OLLAMA.is_local
    assert ProviderName.VLLM.is_local
    assert ProviderName.NONE.is_local
    assert not ProviderName.OPENAI.is_local
    assert not ProviderName.ANTHROPIC.is_local


def test_severity_rank_order() -> None:
    assert Severity.CRITICAL.rank < Severity.HIGH.rank < Severity.INFO.rank


def test_mandatory_approvals() -> None:
    assert {o.value for o in ApprovalOperation.mandatory()} == {
        "delete", "migration", "merge", "deploy",
    }  # fmt: skip
