"""LLM guard: policy, approval, budget, redaction, schema validation (FR-15, FR-18)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from ai_coding.config.models import BudgetsConfig
from ai_coding.domain.enums import ProviderName
from ai_coding.llm import (
    GuardedLLM,
    InvalidModelOutputError,
    LLMApprovalRequiredError,
    LLMBudgetExceededError,
    LLMPolicyDeniedError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
)
from ai_coding.orchestrator.budget import BudgetTracker
from ai_coding.orchestrator.policy import PolicyEngine
from tests.helpers import make_config

SECRET = "sk-ant-api03-" + "Z" * 40


@dataclass
class FakeProvider:
    """Test double: records requests, returns a scripted reply."""

    name: ProviderName = ProviderName.OLLAMA
    base_url: str | None = "http://localhost:11434"
    reply: str = '{"ok": true}'
    seen: list[LLMRequest] = field(default_factory=list)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.seen.append(request)
        return LLMResponse(self.reply, request.model, TokenUsage(10, 5), cost_usd=0.01)


def ollama_policy() -> PolicyEngine:
    return PolicyEngine(make_config(ai__enabled=True, ai__provider="ollama", ai__model="m"))


def request(content: str = "hi", schema: dict | None = None) -> LLMRequest:  # type: ignore[type-arg]
    return LLMRequest("m", (Message("user", content, untrusted=True),), max_output_tokens=10,
                      response_schema=schema)  # fmt: skip


def test_fake_satisfies_port() -> None:
    assert isinstance(FakeProvider(), LLMProvider)


async def test_secrets_never_reach_the_provider_and_usage_is_recorded() -> None:
    provider = FakeProvider(reply=f"echo {SECRET}")
    usage: list[tuple[ProviderName, str, int, float]] = []
    budget = BudgetTracker(BudgetsConfig())
    guard = GuardedLLM(
        provider,
        policy=ollama_policy(),
        budget=budget,
        record_usage=lambda p, m, u, c: usage.append((p, m, u.total, c)),
    )
    response = await guard.complete(request(f"key is {SECRET}"))
    assert SECRET not in provider.seen[0].messages[0].content
    assert provider.seen[0].messages[0].untrusted
    assert SECRET not in response.text
    assert usage == [(ProviderName.OLLAMA, "m", 15, 0.01)]
    assert budget.snapshot().tokens == 15


async def test_policy_denial() -> None:
    guard = GuardedLLM(FakeProvider(), policy=PolicyEngine(make_config()),
                       budget=BudgetTracker(BudgetsConfig()))  # fmt: skip
    with pytest.raises(LLMPolicyDeniedError, match="AI is disabled"):
        await guard.complete(request())


async def test_external_provider_needs_approval() -> None:
    policy = PolicyEngine(
        make_config(
            ai__enabled=True, ai__provider="anthropic", ai__model="m", ai__api_key_env="K",
            security__network="allowlist", security__egress_allowlist=["api.anthropic.com"],
        )
    )  # fmt: skip
    provider = FakeProvider(name=ProviderName.ANTHROPIC, base_url=None)
    guard = GuardedLLM(provider, policy=policy, budget=BudgetTracker(BudgetsConfig()))
    with pytest.raises(LLMApprovalRequiredError):
        await guard.complete(request())
    assert provider.seen == []


async def test_budget_precheck() -> None:
    provider = FakeProvider()
    guard = GuardedLLM(provider, policy=ollama_policy(),
                       budget=BudgetTracker(BudgetsConfig(max_tokens_per_run=5)))  # fmt: skip
    with pytest.raises(LLMBudgetExceededError):
        await guard.complete(request("x" * 100))
    assert provider.seen == []


SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


async def test_structured_output_validated() -> None:
    guard = GuardedLLM(
        FakeProvider(), policy=ollama_policy(), budget=BudgetTracker(BudgetsConfig())
    )
    response = await guard.complete(request(schema=SCHEMA))
    assert response.parsed == {"ok": True}


@pytest.mark.parametrize(
    ("reply", "message"),
    [("not json", "not JSON"), (json.dumps({"ok": "yes"}), "violates schema at ok")],
)
async def test_invalid_output_is_an_error(reply: str, message: str) -> None:
    guard = GuardedLLM(FakeProvider(reply=reply), policy=ollama_policy(),
                       budget=BudgetTracker(BudgetsConfig()))  # fmt: skip
    with pytest.raises(InvalidModelOutputError, match=message):
        await guard.complete(request(schema=SCHEMA))


def test_token_usage_total() -> None:
    assert TokenUsage(3, 4).total == 7
    assert Message("user", "a").with_content("b").content == "b"
