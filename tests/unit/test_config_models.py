"""Tests for the typed configuration model and its security rules."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import (
    ApprovalOperation,
    ExecutionBackend,
    Mode,
    NetworkPolicy,
    ProviderName,
    Severity,
)


def base(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {"schema_version": "2.1", "project": {"name": "demo"}}
    doc.update(overrides)
    return doc


def test_minimal_config_has_secure_defaults() -> None:
    cfg = AICodingConfig.model_validate(base())
    assert cfg.mode is Mode.AUDIT
    assert cfg.ai.enabled is False
    assert cfg.ai.provider is ProviderName.NONE
    assert cfg.execution.backend is ExecutionBackend.LOCAL
    assert cfg.execution.allow_host_commands is False
    assert cfg.execution.use_worktree is True
    assert cfg.security.network is NetworkPolicy.LOCAL_ONLY
    assert cfg.security.redact_secrets is True
    assert cfg.approvals.auto_merge is False
    assert ApprovalOperation.mandatory() <= set(cfg.approvals.required_for)
    gates = cfg.quality_gates
    assert all(g.enabled and g.blocking for g in (gates.build, gates.tests, gates.static))
    assert gates.security.fail_on is Severity.HIGH
    assert gates.review.ai_review_blocking is False
    assert len(cfg.agents.enabled) == 12


def test_config_is_immutable() -> None:
    cfg = AICodingConfig.model_validate(base())
    with pytest.raises(ValidationError):
        cfg.mode = Mode.CREATE  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"unknown": 1}, "Extra inputs"),
        ({"schema_version": "1.0"}, "schema_version"),
        ({"project": {"name": "bad name!"}}, "pattern"),
        ({"stacks": ["python", "python"]}, "duplicates"),
        ({"ai": {"enabled": True}}, "provider is 'none'"),
        ({"ai": {"enabled": True, "provider": "ollama"}}, "ai.model is required"),
        (
            {"ai": {"enabled": True, "provider": "openai", "model": "gpt"}},
            "api_key_env is required",
        ),
        ({"ai": {"api_key_env": "lower_case"}}, "pattern"),
        ({"security": {"network": "allowlist"}}, "must not be empty"),
        ({"security": {"egress_allowlist": ["a.example.com"]}}, "must be empty"),
        ({"security": {"redact_secrets": False}}, "Input should be True"),
        ({"approvals": {"required_for": ["merge"]}}, "mandatory operations"),
        ({"approvals": {"required_for": ["merge"] * 5}}, "mandatory"),
        ({"approvals": {"auto_merge": True}}, "Input should be False"),
        ({"artifacts": {"path": "../outside"}}, "must not escape"),
        ({"project": {"name": "x", "default_branch": "dev"}}, "must be covered"),
        ({"agents": {"enabled": ["qa", "qa"]}}, "duplicates"),
        ({"execution": {"limits": {"timeout_seconds": 0}}}, "greater than or equal"),
    ],
)
def test_invalid_configs_are_rejected(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError) as info:
        AICodingConfig.model_validate(base(**overrides))
    assert fragment in str(info.value)


def test_duplicate_approvals_rejected() -> None:
    ops = [*sorted(ApprovalOperation.mandatory()), ApprovalOperation.DELETE]
    with pytest.raises(ValidationError, match="duplicates"):
        AICodingConfig.model_validate(base(approvals={"required_for": ops}))


def test_external_provider_forbidden_in_local_only() -> None:
    doc = base(ai={"enabled": True, "provider": "anthropic", "model": "m", "api_key_env": "K"})
    with pytest.raises(ValidationError, match="forbids egress"):
        AICodingConfig.model_validate(doc)


def test_external_fallback_forbidden_in_local_only() -> None:
    doc = base(
        ai={
            "enabled": True,
            "provider": "ollama",
            "model": "m",
            "fallback": [{"provider": "openai", "model": "x"}],
        }
    )
    with pytest.raises(ValidationError, match="openai"):
        AICodingConfig.model_validate(doc)


def test_local_model_allowed_offline() -> None:
    """AC-05: local model with no egress is a valid configuration."""
    doc = base(ai={"enabled": True, "provider": "ollama", "model": "qwen2.5-coder"})
    cfg = AICodingConfig.model_validate(doc)
    assert cfg.ai.providers_in_use() == {ProviderName.OLLAMA}


def test_disabled_ai_uses_no_providers() -> None:
    cfg = AICodingConfig.model_validate(base(ai={"provider": "openai"}))
    assert cfg.ai.providers_in_use() == set()


def test_github_actions_requires_network() -> None:
    with pytest.raises(ValidationError, match="requires network"):
        AICodingConfig.model_validate(base(execution={"backend": "github-actions"}))
    cfg = AICodingConfig.model_validate(
        base(
            execution={"backend": "github-actions"},
            security={"network": "allowlist", "egress_allowlist": ["api.github.com"]},
        )
    )
    assert cfg.execution.backend is ExecutionBackend.GITHUB_ACTIONS


def test_audit_mode_is_read_only() -> None:
    with pytest.raises(ValidationError, match="read-only"):
        AICodingConfig.model_validate(base(execution={"allow_host_commands": True}))
    cfg = AICodingConfig.model_validate(
        base(mode="develop", execution={"allow_host_commands": True})
    )
    assert cfg.execution.allow_host_commands


def test_absolute_artifact_path_allowed() -> None:
    cfg = AICodingConfig.model_validate(base(artifacts={"path": "/var/lib/ai-coding"}))
    assert cfg.artifacts.path == "/var/lib/ai-coding"
    cfg = AICodingConfig.model_validate(base(artifacts={"path": "C:\\data"}))
    assert cfg.artifacts.path == "C:\\data"


def test_protected_branch_glob_covers_default() -> None:
    cfg = AICodingConfig.model_validate(
        base(
            project={
                "name": "x",
                "default_branch": "release/2.1",
                "protected_branches": ["release/*"],
            }
        )
    )
    assert cfg.project.default_branch == "release/2.1"
