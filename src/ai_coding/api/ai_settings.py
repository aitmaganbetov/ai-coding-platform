"""The orchestrator's AI provider configured from the Web UI (ADR-0046).

The project's file (``.ai-coding.yml``) stays the base; the ``ai`` settings section made
in the Web UI overrides it field by field for server runs (a field left empty inherits
the file's value). The API key is the project secret ``ai.api_key`` (encrypted, write-
only, see :mod:`ai_coding.api.project_settings`): the effective configuration names it
by a variable that exists in no environment, and the LLM factory resolves that name
through :data:`ai_coding.agents.llm_factory.CREDENTIALS` when a run needs the model — the
key is never in ``os.environ``, run options, artifacts or answers.

*Private Mode* is ``security.network: local-only``: external providers are refused (the
file's policy otherwise; turning it off in the web allows the provider's host only).

A project's configuration may not name a variable starting with ``AI_CODING_`` as a model
key, except its own web key: on the server those hold the platform's secrets (database,
secret store key, S3, the web keys of other projects), and a run would send the value to
the project's own model endpoint.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_coding.agents.catalog import CATALOG
from ai_coding.agents.llm_factory import CREDENTIALS, api_key
from ai_coding.api.core import ForbiddenError, InvalidError, Principal, detached_project
from ai_coding.api.project_settings import ProjectSettingsService
from ai_coding.api.rbac import Permission, allowed
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import AgentName, NetworkPolicy, Outcome, ProviderName
from ai_coding.llm.providers import ANTHROPIC_BASE_URL, OLLAMA_BASE_URL, OPENAI_BASE_URL
from ai_coding.orchestrator.policy import PROVIDER_HOSTS, PolicyEngine
from ai_coding.persistence.models import Project
from ai_coding.security import Redactor
from ai_coding.security.endpoints import is_local_endpoint

SECTION = "ai"
KEY_SECRET = "ai.api_key"  # noqa: S105 - the secret's name, not its value
ANTHROPIC_VERSION = "2023-06-01"
PROBE_TIMEOUT = 10.0
PROBE_LIMIT = 1024 * 1024
Provider = Literal["openai", "anthropic", "ollama", "vllm"]
DEFAULT_URLS = {
    "openai": OPENAI_BASE_URL,
    "anthropic": ANTHROPIC_BASE_URL,
    "ollama": OLLAMA_BASE_URL,
}
LLM_AGENTS = tuple(spec.name.value for spec in CATALOG if spec.requires_llm)
FIELDS = ("enabled", "provider", "model", "base_url", "routing", "fallback", "budgets", "private")
PLATFORM_PREFIX = "AI_CODING_"  # the platform's own variables and the web keys' names


class BudgetsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_tokens_per_run: int | None = Field(default=None, ge=0, le=100_000_000)
    max_cost_usd_per_run: float | None = Field(default=None, ge=0, le=100_000)
    max_duration_minutes: int | None = Field(default=None, ge=1, le=10080)


class AISettings(BaseModel):
    """The ``ai`` section: every field optional — empty inherits the project's file."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    provider: Provider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2048)
    routing: dict[AgentName, str] | None = None  # agent → model of the same provider
    fallback: list[str] | None = Field(default=None, max_length=5)
    budgets: BudgetsIn | None = None
    private: bool | None = None

    @model_validator(mode="after")
    def _checks(self) -> AISettings:
        if (
            self.base_url
            and self.base_url.startswith("http://")
            and not is_local_endpoint(self.base_url)
        ):
            raise ValueError("a remote endpoint must use https (http only for local hosts)")
        for model in [*(self.routing or {}).values(), *(self.fallback or [])]:
            if not model or len(model) > 200:
                raise ValueError("a model name is 1 to 200 characters")
        return self


def key_env_name(project_id: uuid.UUID) -> str:
    """The name the effective configuration gives the web key (in no environment)."""
    return f"AI_CODING_PROJECT_{project_id.hex[:16].upper()}_API_KEY"


def platform_names(config: AICodingConfig, *, own: str | None) -> list[str]:
    """Key variables of ``config`` that belong to the platform (all but the project's own
    web key): resolving them would hand the platform's secrets to the model endpoint."""
    ai = config.ai
    names = {ai.api_key_env, *(r.api_key_env for r in (*ai.routing.values(), *ai.fallback))}
    return sorted(n for n in names if n and n.startswith(PLATFORM_PREFIX) and n != own)


def _refusal(names: list[str]) -> str:
    return (
        f"ai: {', '.join(names)}: variables starting with {PLATFORM_PREFIX} belong to the "
        "platform and cannot hold a model key; set the key in the Web UI (Models) or name "
        "another variable"
    )


def refuse_platform_names(config: AICodingConfig, *, own: str | None) -> None:
    if names := platform_names(config, own=own):
        raise InvalidError(_refusal(names))


def _host(provider: str, base_url: str | None) -> str | None:
    if base_url:
        return urlsplit(base_url).netloc or None
    return PROVIDER_HOSTS.get(ProviderName(provider))


def apply_ai(config: AICodingConfig, web: AISettings, *, key_env: str | None) -> AICodingConfig:
    """The file's configuration with the web settings on top (validated as a whole)."""
    if web == AISettings() and key_env is None:
        return config
    data = config.model_dump(mode="json", exclude_none=True)
    ai: dict[str, Any] = data.setdefault("ai", {})
    file_provider = ai.get("provider")
    if web.enabled is not None:
        ai["enabled"] = web.enabled
    if web.provider is not None and web.provider != file_provider:
        # Another provider: the file's endpoint, key and routes were for the old one.
        ai["provider"] = web.provider
        for stale in ("base_url", "api_key_env", "routing", "fallback"):
            ai.pop(stale, None)
    if web.model is not None:
        ai["model"] = web.model
    if web.base_url is not None:
        ai["base_url"] = web.base_url
    provider = ai.get("provider", "none")
    if key_env is not None and provider in ("openai", "anthropic", "vllm"):
        ai["api_key_env"] = key_env
    if web.routing is not None:
        ai["routing"] = {
            agent.value: {"provider": provider, "model": model}
            for agent, model in web.routing.items()
        }
    if web.fallback is not None:
        ai["fallback"] = [{"provider": provider, "model": model} for model in web.fallback]
    if web.budgets is not None:
        budgets = data.setdefault("budgets", {})
        budgets.update({k: v for k, v in web.budgets.model_dump().items() if v is not None})
    _apply_private(data.setdefault("security", {}), ai, web.private)
    return AICodingConfig.model_validate(data)


def _apply_private(security: dict[str, Any], ai: dict[str, Any], private: bool | None) -> None:
    """Private Mode on: local-only. Off: the file's policy, letting the provider's host
    through (the only host it adds)."""
    if private is True:
        security.update({"network": NetworkPolicy.LOCAL_ONLY.value, "egress_allowlist": []})
        return
    provider = ai.get("provider", "none")
    if (
        private is not False
        or not ai.get("enabled")
        or provider not in ("openai", "anthropic", "vllm")
    ):
        return
    host = _host(provider, ai.get("base_url"))
    network = security.get("network", NetworkPolicy.LOCAL_ONLY.value)
    local = provider == "vllm" and is_local_endpoint(ai.get("base_url", ""))
    if not host or local or network == NetworkPolicy.OPEN.value:
        return
    allow = security.get("egress_allowlist", []) if network == NetworkPolicy.ALLOWLIST.value else []
    security.update({"network": NetworkPolicy.ALLOWLIST.value,
                     "egress_allowlist": sorted({*allow, host})})  # fmt: skip


def _view(config: AICodingConfig) -> dict[str, Any]:
    ai = config.ai
    return {
        "enabled": ai.enabled,
        "provider": ai.provider.value,
        "model": ai.model,
        "base_url": ai.base_url,
        "routing": {agent.value: route.model for agent, route in ai.routing.items()},
        "fallback": [route.model for route in ai.fallback],
        "budgets": config.budgets.model_dump(),
        "private": config.security.network is NetworkPolicy.LOCAL_ONLY,
        "network": config.security.network.value,
    }


def _probe_guard(url: str) -> None:
    """The API calls the endpoint itself: never cloud metadata or other link-local hosts."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidError("the endpoint must be an http(s) URL")
    host = parts.hostname.lower()
    if host in ("metadata.google.internal", "metadata", "instance-data"):
        raise InvalidError("this endpoint cannot be called")
    try:
        addresses = {
            str(info[4][0])
            for info in socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        }
    except OSError as exc:
        raise InvalidError(f"{host} does not resolve ({exc})") from None
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise InvalidError("this endpoint cannot be called")


class AIProviderService(ProjectSettingsService):
    """Reads, changes and checks the AI provider of a project's orchestrator."""

    # ---------------------------------------------------- effective config --
    def config_overrides(self, project: Project, config: AICodingConfig) -> AICodingConfig:
        config = super().config_overrides(project, config)
        web = self._web_ai(project.id)
        key_env = self._key_env(project.id, web, config)
        effective = config
        if web != AISettings() or key_env is not None:
            try:
                effective = apply_ai(config, web, key_env=key_env)
            except ValidationError:
                # Settings that no longer fit the file (changed since): the file decides
                # until they are fixed — GET /ai reports it.
                effective = config
        # Every server path to a model goes through here: the file may not name the
        # platform's variables, nor another project's web key (the credentials are
        # shared by the whole process and resolved by name).
        refuse_platform_names(effective, own=key_env)
        if key_env is not None:
            project_id = project.id
            CREDENTIALS.register(key_env, lambda: self.secret_value(project_id, KEY_SECRET))
        return effective

    def _web_ai(self, project_id: uuid.UUID) -> AISettings:
        try:
            return AISettings.model_validate(self.section(project_id, SECTION))
        except ValidationError:
            return AISettings()

    def _key_env(self, project_id: uuid.UUID, web: AISettings, base: AICodingConfig) -> str | None:
        """The web key's name when it applies: a key is set in the web, or the web chose
        another provider (the file's variable held the old provider's key)."""
        switched = web.provider is not None and web.provider != base.ai.provider.value
        return key_env_name(project_id) if self._web_key_set(project_id) or switched else None

    def _web_key_set(self, project_id: uuid.UUID) -> bool:
        from ai_coding.persistence.models import ProjectSecret  # noqa: PLC0415

        with self.session() as s:
            return s.get(ProjectSecret, (project_id, KEY_SECRET)) is not None

    # ----------------------------------------------------------------- read --
    def ai_settings(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        """The file's values, the web's, the effective ones and where each comes from."""
        with self.session() as s:
            role = self.require(s, principal, project_id, Permission.PROJECT_VIEW)
            project = detached_project(s, project_id)
        stored = self.read_section(principal, project_id, SECTION)
        web = self._web_ai(project_id)
        _root, base = self.file_config(project)
        key_env = self._key_env(project_id, web, base)
        error = None
        try:
            effective = apply_ai(base, web, key_env=key_env)
        except ValidationError as exc:
            effective, error = (
                base,
                "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors()),
            )
        given = web.model_dump(exclude_none=True)
        refused = platform_names(effective, own=key_env)
        if refused:
            error = "; ".join(filter(None, (error, _refusal(refused))))
        env = effective.ai.api_key_env
        if env and env == key_env_name(project_id):
            key = {"set": self._web_key_set(project_id), "source": "web", "env": None}
        elif env in refused:  # refused: not even whether the server has it
            key = {"set": False, "source": "env", "env": env}
        elif env:  # a variable of the server's environment: its name only, never its value
            key = {"set": bool(os.environ.get(env)), "source": "env", "env": env}
        else:
            key = {"set": False, "source": None, "env": None}
        return {
            "settings": given,
            "version": stored["version"],
            "updated_by": stored["updated_by"],
            "updated_at": stored["updated_at"],
            "file": _view(base),
            "effective": _view(effective),
            "sources": {name: ("web" if name in given else "file") for name in FIELDS},
            "key": key,
            "valid": error is None,
            "error": error,
            "can_edit": allowed(role, Permission.PROJECT_SETTINGS),
            "secrets_available": self.secrets_available,
            "agents": list(LLM_AGENTS),
        }

    # --------------------------------------------------------------- change --
    def update_ai_settings(
        self, principal: Principal, project_id: uuid.UUID, data: dict[str, Any], version: int | None
    ) -> dict[str, Any]:
        with self.session() as s:  # access first
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            project = detached_project(s, project_id)
        try:
            web = AISettings.model_validate(data)
        except ValidationError as exc:
            raise InvalidError(
                "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
            ) from None
        _root, base = self.file_config(project)
        try:
            apply_ai(base, web, key_env=self._key_env(project_id, web, base))
        except ValidationError as exc:
            reasons = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
            raise InvalidError(
                f"the AI settings do not fit the project's configuration: {reasons}"
            ) from None
        stored = web.model_dump(mode="json", exclude_none=True)
        self.write_section(principal, project_id, SECTION, stored, version=version)
        return self.ai_settings(principal, project_id)

    # ---------------------------------------------------------------- probe --
    def _probe_target(
        self, principal: Principal, project_id: uuid.UUID
    ) -> tuple[str, str, str | None, str | None]:
        """(provider, model, base url, key) of the effective configuration, policy-checked."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            project = detached_project(s, project_id)
        _root, config = self.project_config(project)
        ai = config.ai
        if ai.provider is ProviderName.NONE:
            raise InvalidError("choose a provider first")
        base_url = ai.base_url or DEFAULT_URLS.get(ai.provider.value)
        if not base_url:
            raise InvalidError("the provider needs an endpoint (base URL)")
        host = urlsplit(base_url).netloc
        local = ai.provider.is_local and is_local_endpoint(base_url)
        if not local:
            decision = PolicyEngine(config).network(host)
            if not decision.allowed:
                raise ForbiddenError(
                    f"the project's network policy refuses {host}: {decision.reason}"
                )
        # The web key (server credentials) or a variable of the server's environment.
        return ai.provider.value, ai.model or "", base_url, api_key(os.environ, ai.api_key_env)

    def _list_models(self, provider: str, base_url: str, key: str | None) -> list[str]:
        url = (
            f"{base_url.rstrip('/')}/v1/models"
            if provider == "anthropic"
            else f"{base_url.rstrip('/')}/models"
        )
        _probe_guard(url)
        headers = {"Accept": "application/json"}
        if provider == "anthropic":
            headers["anthropic-version"] = ANTHROPIC_VERSION
            if key:
                headers["x-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            response = client.get(url, headers=headers)
        body = response.content[:PROBE_LIMIT].decode(errors="replace")
        if response.status_code >= 400:
            message = body
            try:
                payload = response.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                message = (error.get("message") if isinstance(error, dict) else error) or str(
                    payload
                )
            except ValueError:
                pass
            redactor = Redactor()
            redactor.add_literals([key] if key else [])
            raise InvalidError(
                f"HTTP {response.status_code}: {redactor.redact(str(message))[:300]}"
            )
        payload = response.json()
        items = (
            payload.get("data") or payload.get("models") or [] if isinstance(payload, dict) else []
        )
        names = [
            str(i.get("id") or i.get("name") or i.get("model"))
            for i in items
            if isinstance(i, dict)
        ]
        return sorted({n for n in names if n and n != "None"})

    def ai_models(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        provider, _model, base_url, key = self._probe_target(principal, project_id)
        try:
            models = self._list_models(provider, base_url or "", key)
        except InvalidError as exc:
            return {"provider": provider, "models": [], "reason": exc.message}
        except httpx.HTTPError as exc:
            return {
                "provider": provider,
                "models": [],
                "reason": f"{type(exc).__name__}: the provider did not answer",
            }
        return {
            "provider": provider,
            "models": models,
            "reason": None if models else "the provider lists no models",
        }

    def test_ai(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        """A real call (the provider's model list) with the effective settings."""
        provider, model, base_url, key = self._probe_target(principal, project_id)
        started = time.monotonic()
        ok, detail, count = False, "", 0
        try:
            models = self._list_models(provider, base_url or "", key)
            count = len(models)
            ok = not models or model in models
            detail = (
                "" if ok else f"model '{model}' is not offered by the provider ({count} available)"
            )
        except InvalidError as exc:
            detail = exc.message
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}: the provider did not answer"
        latency = round((time.monotonic() - started) * 1000)
        with self.session() as s:
            self.record(s, principal.username, "project.ai.test",
                        Outcome.SUCCESS if ok else Outcome.FAILURE, project_id=project_id,
                        target=f"{provider}/{model}")  # fmt: skip
        return {"ok": ok, "provider": provider, "model": model, "latency_ms": latency,
                "models_offered": count, "detail": detail}  # fmt: skip


__all__ = [
    "KEY_SECRET",
    "AIProviderService",
    "AISettings",
    "apply_ai",
    "key_env_name",
    "platform_names",
    "refuse_platform_names",
]
