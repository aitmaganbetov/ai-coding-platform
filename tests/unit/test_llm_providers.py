"""Provider adapters, response cache and routing/fallback (S5, ADR-0023). No network."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_coding.config.models import BudgetsConfig
from ai_coding.domain.enums import ProviderName
from ai_coding.domain.errors import TransientError
from ai_coding.llm import (
    AllRoutesFailedError,
    AllRoutesUnavailableError,
    AnthropicProvider,
    GuardedLLM,
    LLMApprovalRequiredError,
    LLMBudgetExceededError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    OllamaProvider,
    OpenAIProvider,
    ProviderRequestError,
    ProviderUnavailableError,
    ResponseCache,
    Route,
    RoutedLLM,
    TokenUsage,
    TruncatedOutputError,
    VLLMProvider,
    make_provider,
)
from ai_coding.orchestrator.budget import BudgetTracker
from ai_coding.orchestrator.policy import PolicyEngine
from ai_coding.security import redact
from tests.helpers import make_config

KEY = "sk-proj-" + "k" * 40
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def req(schema: dict[str, Any] | None = SCHEMA, temperature: float = 0.0) -> LLMRequest:
    return LLMRequest(
        model="m1",
        messages=(Message("system", "be brief"), Message("user", "hi")),
        max_output_tokens=50,
        temperature=temperature,
        response_schema=schema,
    )


Handler = Callable[[httpx.Request], httpx.Response]


def transport(handler: Handler, seen: list[httpx.Request]) -> httpx.MockTransport:
    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def openai_reply(content: str, finish: str = "stop", **extra: Any) -> httpx.Response:
    body = {
        "model": "m1-2026",
        "choices": [{"message": {"role": "assistant", "content": content, **extra},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }  # fmt: skip
    return httpx.Response(200, json=body)


# ------------------------------------------------------------------ OpenAI --
async def test_openai_payload_structured_output_usage_and_cost() -> None:
    seen: list[httpx.Request] = []
    p = OpenAIProvider(
        api_key=KEY,
        pricing={"m1-2026": (2.0, 8.0)},
        transport=transport(lambda r: openai_reply('{"answer": "ok"}'), seen),
    )
    assert isinstance(p, LLMProvider) and p.base_url == "https://api.openai.com/v1"
    out = await p.complete(req())
    sent = seen[0]
    assert str(sent.url) == "https://api.openai.com/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["max_completion_tokens"] == 50 and "max_tokens" not in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert out.text == '{"answer": "ok"}' and out.model == "m1-2026"
    assert out.usage == TokenUsage(120, 30)
    assert out.cost_usd == pytest.approx(120 * 2 / 1e6 + 30 * 8 / 1e6)
    assert KEY not in redact(f"key {KEY}")  # key registered with the redactor


async def test_openai_compatible_local_servers() -> None:
    seen: list[httpx.Request] = []
    ollama = OllamaProvider(transport=transport(lambda r: openai_reply("x"), seen))
    await ollama.complete(req(schema=None))
    assert str(seen[0].url) == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in seen[0].headers
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 50 and "response_format" not in body
    vllm = VLLMProvider(base_url="http://gpu.internal:8000/v1/", transport=transport(
        lambda r: openai_reply("x"), seen))  # fmt: skip
    await vllm.complete(req(schema=None))
    assert str(seen[1].url) == "http://gpu.internal:8000/v1/chat/completions"
    assert vllm.cost("m1-2026", TokenUsage(10, 10)) == 0.0  # unpriced
    with pytest.raises(ValueError, match="needs a base_url"):
        VLLMProvider()


def test_external_providers_require_https() -> None:
    with pytest.raises(ValueError, match="must use https"):
        OpenAIProvider(base_url="http://proxy.example/v1", api_key=KEY)
    with pytest.raises(ValueError, match="scheme"):
        AnthropicProvider(base_url="ftp://x", api_key=KEY)
    OpenAIProvider(base_url="http://localhost:8080/v1")  # loopback test servers are fine
    with pytest.raises(ValueError, match="no adapter"):
        make_provider(ProviderName.NONE)
    assert isinstance(make_provider(ProviderName.OLLAMA), OllamaProvider)


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (openai_reply("x", finish="length"), TruncatedOutputError, "truncated"),
        (openai_reply("", refusal="no"), ProviderRequestError, "refused"),
        (httpx.Response(200, json={"choices": []}), ProviderRequestError, "no choices"),
        (httpx.Response(200, text="<html>"), ProviderRequestError, "not JSON"),
        (httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}),
         ProviderRequestError, r"HTTP 401: bad key \*\*\*REDACTED"),
        (httpx.Response(404, json={"error": {"message": "no such model"}}),
         ProviderRequestError, "no such model"),
        (httpx.Response(429, json={"error": {"message": "slow down"}},
                        headers={"retry-after": "7"}), ProviderUnavailableError, "429"),
        (httpx.Response(503, text="overloaded"), ProviderUnavailableError, "503"),
    ],
)  # fmt: skip
async def test_openai_error_classification(
    response: httpx.Response, error: type[Exception], match: str
) -> None:
    p = OpenAIProvider(api_key=KEY, transport=transport(lambda r: response, []))
    with pytest.raises(error, match=match) as info:
        await p.complete(req())
    if isinstance(info.value, ProviderUnavailableError) and "429" in str(info.value):
        assert info.value.retry_after == 7.0
    assert KEY not in str(info.value)


async def test_network_failures_are_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    for handler, match in ((boom, "unreachable"), (slow, "timed out")):
        p = OllamaProvider(transport=transport(handler, []))
        with pytest.raises(ProviderUnavailableError, match=match) as info:
            await p.complete(req())
        assert isinstance(info.value, TransientError)


# --------------------------------------------------------------- Anthropic --
def anthropic_reply(blocks: list[dict[str, Any]], stop: str = "tool_use") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "a1", "stop_reason": stop, "content": blocks,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5},
    })  # fmt: skip


async def test_anthropic_forced_tool_call_for_structured_output() -> None:
    seen: list[httpx.Request] = []
    reply = anthropic_reply([
        {"type": "text", "text": "thinking aloud"},
        {"type": "tool_use", "name": "submit_result", "input": {"answer": "ok"}},
    ])  # fmt: skip
    p = AnthropicProvider(api_key=KEY, pricing={"a1": (3.0, 15.0)},
                          transport=transport(lambda r: reply, seen))  # fmt: skip
    out = await p.complete(req())
    sent = seen[0]
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == KEY and sent.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(sent.content)
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": "submit_result"}
    assert json.loads(out.text) == {"answer": "ok"}
    assert out.usage == TokenUsage(105, 20)
    assert out.cost_usd == pytest.approx(105 * 3 / 1e6 + 20 * 15 / 1e6)


async def test_anthropic_text_and_errors() -> None:
    text = anthropic_reply(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "end_turn"
    )
    p = AnthropicProvider(api_key=KEY, transport=transport(lambda r: text, []))
    assert (await p.complete(req(schema=None))).text == "ab"
    for response, error in (
        (anthropic_reply([], "max_tokens"), TruncatedOutputError),
        (anthropic_reply([], "refusal"), ProviderRequestError),
        (anthropic_reply([], "end_turn"), ProviderRequestError),
        (httpx.Response(529, json={"error": {"message": "overloaded"}}), ProviderUnavailableError),
    ):
        bad = AnthropicProvider(api_key=KEY, transport=transport(lambda r, x=response: x, []))
        with pytest.raises(error):
            await bad.complete(req())


# ------------------------------------------------------------------- cache --
@dataclass
class Scripted:
    """Provider double returning scripted replies or raising scripted errors."""

    name: ProviderName = ProviderName.OLLAMA
    base_url: str | None = "http://localhost:11434/v1"
    replies: list[str | Exception] = field(default_factory=list)
    calls: int = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        item = self.replies.pop(0) if self.replies else '{"answer": "default"}'
        if isinstance(item, Exception):
            raise item
        return LLMResponse(item, request.model, TokenUsage(10, 5), cost_usd=0.001)


def policy(**overrides: Any) -> PolicyEngine:
    cfg = {"ai__enabled": True, "ai__provider": "ollama", "ai__model": "m1", **overrides}
    return PolicyEngine(make_config(**cfg))


async def test_cache_reuses_validated_deterministic_answers(tmp_path: Path) -> None:
    provider = Scripted(replies=['{"answer": "first"}', '{"answer": "second"}'])
    budget = BudgetTracker(BudgetsConfig())
    usage: list[Any] = []
    cache = ResponseCache(tmp_path / "cache")
    guard = GuardedLLM(provider, policy=policy(), budget=budget, cache=cache,
                       record_usage=lambda *a: usage.append(a))  # fmt: skip
    first = await guard.complete(req())
    second = await guard.complete(req())
    assert provider.calls == 1 and not first.cached and second.cached
    assert second.parsed == {"answer": "first"} and second.usage.total == 0
    assert len(usage) == 1  # a hit is not billed
    hot = await guard.complete(req(temperature=0.7))  # non-deterministic: never cached
    assert provider.calls == 2 and not hot.cached
    other_schema = {**SCHEMA, "title": "x"}
    await guard.complete(req(schema=other_schema))
    assert provider.calls == 3


async def test_invalid_answers_are_not_cached_and_corrupt_entries_are_misses(
    tmp_path: Path,
) -> None:
    cache = ResponseCache(tmp_path)
    provider = Scripted(replies=["not json", '{"answer": "ok"}'])
    guard = GuardedLLM(provider, policy=policy(), budget=BudgetTracker(BudgetsConfig()),
                       cache=cache)  # fmt: skip
    with pytest.raises(Exception, match="not JSON"):
        await guard.complete(req())
    assert (await guard.complete(req())).parsed == {"answer": "ok"}
    for entry in tmp_path.rglob("*.json"):
        entry.write_text("{broken", encoding="utf-8")
    assert cache.get(next(tmp_path.rglob("*.json")).stem) is None


async def test_secrets_never_reach_the_cache(tmp_path: Path) -> None:
    secret = "ghp_" + "s" * 36
    provider = Scripted(replies=[json.dumps({"answer": f"echo {secret}"})])
    guard = GuardedLLM(provider, policy=policy(), budget=BudgetTracker(BudgetsConfig()),
                       cache=ResponseCache(tmp_path))  # fmt: skip
    request = LLMRequest("m1", (Message("user", f"token {secret}"),), response_schema=SCHEMA)
    out = await guard.complete(request)
    assert secret not in out.text
    stored = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    assert stored and secret not in stored


# ----------------------------------------------------------------- routing --
async def test_fallback_on_unavailable_invalid_and_rejected() -> None:
    primary = Scripted(replies=[ProviderUnavailableError("down")])
    invalid = Scripted(name=ProviderName.VLLM, base_url="http://localhost:8000/v1",
                       replies=["{}"])  # fmt: skip
    good = Scripted(replies=['{"answer": "fallback"}'])
    seen: list[Any] = []
    llm = RoutedLLM(
        [Route(primary, "m1"), Route(invalid, "m2"), Route(good, "m3")],
        policy=policy(ai__fallback=[{"provider": "vllm", "model": "m2",
                                     "base_url": "http://localhost:8000/v1"}]),
        budget=BudgetTracker(BudgetsConfig()),
        on_attempt=seen.append,
    )  # fmt: skip
    out = await llm.complete(req())
    assert out.parsed == {"answer": "fallback"} and out.model == "m3"
    assert [a.route for a in llm.attempts] == ["ollama/m1", "vllm/m2"]
    assert llm.attempts[0].transient and not llm.attempts[1].transient
    assert "violates schema" in llm.attempts[1].error and len(seen) == 2


async def test_policy_denied_route_is_skipped() -> None:
    external = Scripted(name=ProviderName.OPENAI, base_url="https://api.openai.com/v1")
    local = Scripted(replies=['{"answer": "local"}'])
    llm = RoutedLLM(
        [Route(external, "gpt"), Route(local, "m1")],  # openai not configured → denied
        policy=policy(), budget=BudgetTracker(BudgetsConfig()),
    )  # fmt: skip
    assert (await llm.complete(req())).parsed == {"answer": "local"}
    assert external.calls == 0 and "not configured" in llm.attempts[0].error


async def test_approval_and_budget_stop_the_chain() -> None:
    external = Scripted(name=ProviderName.ANTHROPIC, base_url="https://api.anthropic.com")
    local = Scripted()
    approval_policy = policy(
        ai__provider="anthropic", ai__api_key_env="K", security__network="open",
        approvals__required_for=["external_llm", "delete", "migration", "merge", "deploy"],
        ai__fallback=[{"provider": "ollama", "model": "m1"}],
    )  # fmt: skip
    llm = RoutedLLM([Route(external, "a"), Route(local, "m1")], policy=approval_policy,
                    budget=BudgetTracker(BudgetsConfig()))  # fmt: skip
    with pytest.raises(LLMApprovalRequiredError):
        await llm.complete(req())
    assert local.calls == 0
    tight = RoutedLLM([Route(Scripted(), "m1"), Route(local, "m1")], policy=policy(),
                      budget=BudgetTracker(BudgetsConfig(max_tokens_per_run=10)))  # fmt: skip
    with pytest.raises(LLMBudgetExceededError):
        await tight.complete(req())
    assert local.calls == 0


async def test_all_routes_failed_classification() -> None:
    down = RoutedLLM([Route(Scripted(replies=[ProviderUnavailableError("a")]), "m1"),
                      Route(Scripted(replies=[ProviderUnavailableError("b")]), "m1")],
                     policy=policy(), budget=BudgetTracker(BudgetsConfig()))  # fmt: skip
    with pytest.raises(AllRoutesUnavailableError) as transient:
        await down.complete(req())
    assert isinstance(transient.value, TransientError) and len(transient.value.attempts) == 2
    mixed = RoutedLLM([Route(Scripted(replies=[ProviderUnavailableError("a")]), "m1"),
                       Route(Scripted(replies=[ProviderRequestError("bad key")]), "m1")],
                      policy=policy(), budget=BudgetTracker(BudgetsConfig()))  # fmt: skip
    with pytest.raises(AllRoutesFailedError, match="bad key"):
        await mixed.complete(req())
    with pytest.raises(ValueError, match="at least one route"):
        RoutedLLM([], policy=policy(), budget=BudgetTracker(BudgetsConfig()))
