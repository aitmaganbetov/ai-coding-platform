"""Policy Engine decisions (FR-11, ТЗ §12)."""

from __future__ import annotations

import pytest

from ai_coding.domain.enums import AgentName, ApprovalOperation, Mode, ProviderName
from ai_coding.orchestrator.policy import PolicyEngine, PolicyOptions, Verdict
from tests.helpers import make_config

A, D, R = Verdict.ALLOW, Verdict.DENY, Verdict.REQUIRE_APPROVAL


def engine(mode: Mode = Mode.DEVELOP, **overrides: object) -> PolicyEngine:
    return PolicyEngine(make_config(mode, **overrides))


@pytest.mark.parametrize("op", sorted(ApprovalOperation.mandatory()))
def test_mandatory_operations_always_need_approval(op: ApprovalOperation) -> None:
    decision = engine(approvals__required_for=sorted(ApprovalOperation.mandatory())).operation(op)
    assert decision.verdict is R and decision.operation is op


def test_optional_operation_follows_config() -> None:
    mandatory = sorted(ApprovalOperation.mandatory())
    assert (
        engine(approvals__required_for=mandatory).operation(ApprovalOperation.PR_CREATE).verdict
        is A
    )
    assert engine().operation(ApprovalOperation.PR_CREATE).verdict is R


def test_require_approval_all_makes_everything_guarded() -> None:
    e = engine(approvals__required_for=sorted(ApprovalOperation.mandatory()))
    strict = e.with_options(PolicyOptions(require_approval_all=True))
    assert strict.operation(ApprovalOperation.PR_CREATE).verdict is R
    assert strict.digest != e.digest


def test_granted_approval_allows() -> None:
    e = engine().with_options(PolicyOptions(granted=frozenset({ApprovalOperation.MERGE})))
    decision = e.operation(ApprovalOperation.MERGE)
    assert decision.verdict is A and "granted" in decision.reason


def test_agents_enabled() -> None:
    e = engine(agents__enabled=["reviewer"])
    assert e.agent(AgentName.REVIEWER).verdict is A
    assert e.agent("developer").verdict is D
    assert e.agent("builtin.discovery.git").verdict is A


@pytest.mark.parametrize(
    ("path", "verdict", "rule"),
    [
        ("src/app.py", A, "fs.write"),
        ("../etc/passwd", D, "fs.scope"),
        ("/etc/passwd", D, "fs.scope"),
        (".github/workflows/ci.yml", R, "approvals.critical_paths"),
        ("db/migrations/001.sql", R, "approvals.critical_paths"),
        ("uv.lock", R, "approvals.critical_paths"),
        ("Dockerfile", R, "approvals.critical_paths"),
    ],
)
def test_file_write(path: str, verdict: Verdict, rule: str) -> None:
    decision = engine().file_write(path)
    assert (decision.verdict, decision.rule) == (verdict, rule)


def test_audit_mode_is_read_only() -> None:
    assert engine(Mode.AUDIT).file_write("src/a.py").rule == "mode.audit"


def test_host_commands() -> None:
    assert engine().command("pytest", on_host=False).verdict is A
    assert engine().command("pytest", on_host=True).rule == "execution.allow_host_commands"
    e = engine(execution__allow_host_commands=True, execution__command_allowlist=["pytest"])
    assert e.command("/usr/bin/pytest", on_host=True).verdict is A
    assert e.command("curl", on_host=True).verdict is R
    strict = e.with_options(PolicyOptions(require_approval_all=True))
    assert strict.command("pytest", on_host=True).verdict is R
    no_approval = engine(
        execution__allow_host_commands=True,
        approvals__required_for=sorted(ApprovalOperation.mandatory()),
    )
    assert no_approval.command("curl", on_host=True).verdict is D


@pytest.mark.parametrize(
    ("network", "allowlist", "host", "verdict"),
    [
        ("local-only", [], "api.github.com", D),
        ("local-only", [], "localhost:11434", A),
        ("local-only", [], "127.0.0.1", A),
        ("open", [], "example.com", A),
        ("allowlist", ["*.github.com"], "api.github.com", A),
        ("allowlist", ["*.github.com"], "github.com", A),
        ("allowlist", ["*.github.com"], "evilgithub.com", D),
        ("allowlist", ["pypi.org"], "pypi.org:443", A),
        ("allowlist", ["pypi.org"], "files.pypi.org", D),
    ],
)
def test_network(network: str, allowlist: list[str], host: str, verdict: Verdict) -> None:
    e = engine(security__network=network, security__egress_allowlist=allowlist)
    assert e.network(host).verdict is verdict


def test_external_llm_rules() -> None:
    assert engine().external_llm(ProviderName.OPENAI).rule == "ai.enabled"
    local = engine(ai__enabled=True, ai__provider="ollama", ai__model="m")
    assert local.external_llm(ProviderName.OLLAMA, base_url="http://localhost:11434").verdict is A
    assert local.external_llm(ProviderName.OPENAI).rule == "ai.provider"
    remote = engine(
        ai__enabled=True, ai__provider="anthropic", ai__model="m", ai__api_key_env="K",
        security__network="allowlist", security__egress_allowlist=["api.anthropic.com"],
    )  # fmt: skip
    assert remote.external_llm(ProviderName.ANTHROPIC).verdict is R
    blocked = engine(
        ai__enabled=True, ai__provider="anthropic", ai__model="m", ai__api_key_env="K",
        security__network="allowlist", security__egress_allowlist=["pypi.org"],
    )  # fmt: skip
    assert blocked.external_llm(ProviderName.ANTHROPIC).rule == "security.egress_allowlist"
    trusting = engine(
        ai__enabled=True, ai__provider="anthropic", ai__model="m", ai__api_key_env="K",
        security__network="open", approvals__required_for=sorted(ApprovalOperation.mandatory()),
    )  # fmt: skip
    assert trusting.external_llm(ProviderName.ANTHROPIC).verdict is A


def test_vllm_remote_host_subject_to_egress() -> None:
    e = engine(ai__enabled=True, ai__provider="vllm", ai__model="m")
    assert e.external_llm(ProviderName.VLLM, base_url="http://gpu.internal:8000").verdict is D
