"""The web's AI settings on top of the project's file (ADR-0046) and the server's
credentials the LLM factory resolves keys from."""

from __future__ import annotations

import os
import uuid

import pytest
from pydantic import ValidationError

from ai_coding.agents.llm_factory import CREDENTIALS, api_key, plan_chain
from ai_coding.api.ai_settings import AISettings, BudgetsIn, apply_ai, key_env_name
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import AgentName

FILE = AICodingConfig.model_validate({
    "schema_version": "2.1",
    "project": {"name": "alpha"},
    "ai": {"enabled": True, "provider": "anthropic", "model": "claude-x",
           "api_key_env": "ANTHROPIC_API_KEY",
           "routing": {"reviewer": {"provider": "anthropic", "model": "claude-y"}}},
    "security": {"network": "allowlist", "egress_allowlist": ["api.anthropic.com", "pypi.org"]},
})  # fmt: skip


def test_fields_inherit_or_override() -> None:
    assert apply_ai(FILE, AISettings(), key_env=None) is FILE
    same = apply_ai(
        FILE, AISettings(model="claude-z", budgets=BudgetsIn(max_tokens_per_run=10)), key_env=None
    )
    assert same.ai.model == "claude-z" and same.ai.api_key_env == "ANTHROPIC_API_KEY"
    assert (
        same.ai.routing[AgentName.REVIEWER].model == "claude-y"
        and same.budgets.max_tokens_per_run == 10
    )
    # Another provider: the file's key, endpoint and routes were for the old one.
    other = apply_ai(FILE, AISettings(provider="openai", model="gpt-x", private=False), key_env="K")
    assert other.ai.api_key_env == "K" and other.ai.routing == {}
    assert other.security.egress_allowlist == ["api.anthropic.com", "api.openai.com", "pypi.org"]
    routed = apply_ai(
        FILE,
        AISettings(routing={AgentName.DEVELOPER: "claude-big"}, fallback=["claude-small"]),
        key_env="K",
    )
    assert (
        routed.ai.routing[AgentName.DEVELOPER].model == "claude-big"
        and routed.ai.api_key_env == "K"
    )
    assert [r.model for r in routed.ai.fallback] == ["claude-small"]


def test_private_mode() -> None:
    with pytest.raises(ValidationError, match="local-only"):
        apply_ai(FILE, AISettings(private=True), key_env=None)
    local = apply_ai(
        FILE, AISettings(provider="ollama", model="llama3", private=True), key_env=None
    )
    assert local.security.network.value == "local-only" and local.security.egress_allowlist == []
    with pytest.raises(ValidationError, match="https"):
        AISettings(base_url="http://example.com/v1")
    assert AISettings(base_url="http://10.0.0.5:8000/v1").base_url  # a local host may use http


def test_credentials_resolve_keys_without_the_environment() -> None:
    name = key_env_name(uuid.uuid4())
    config = apply_ai(
        FILE, AISettings(provider="openai", model="gpt-x", private=False), key_env=name
    )
    assert plan_chain(config, AgentName.DEVELOPER, env={}).skipped == (
        f"openai/gpt-x: ${name} is not set",
    )
    CREDENTIALS.register(name, lambda: "sk-from-the-store")
    try:
        assert [r.model for r in plan_chain(config, AgentName.DEVELOPER, env={}).routes] == [
            "gpt-x"
        ]
        assert api_key({}, name) == "sk-from-the-store" and name not in os.environ
        assert api_key({name: "env-first"}, name) == "env-first"
        CREDENTIALS.register(name, lambda: (_ for _ in ()).throw(RuntimeError("broken")))
        assert api_key({}, name) is None  # an unreadable key counts as missing
    finally:
        CREDENTIALS.unregister(name)
