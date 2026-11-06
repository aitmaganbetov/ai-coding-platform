"""local-only means no egress: a local provider type is not enough, its endpoint is checked
(ТЗ §12, AC-05)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ai_coding.agents.llm_factory import LLMUnavailableError, build_llm
from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import AgentName
from ai_coding.security.endpoints import is_local_endpoint, resolves_locally


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("http://10.1.2.3", True),
        ("http://192.168.0.5:11434", True),
        ("http://172.20.0.4", True),
        ("http://169.254.1.1", True),
        ("http://ollama:11434", True),  # Docker/Kubernetes service name
        ("http://gpu.internal", True),
        ("http://box.local", True),
        ("http://ollama.example.com:11434", False),
        ("https://8.8.8.8", False),
        ("http://[2001:4860::8888]", False),
        ("http://[::ffff:10.0.0.1]", True),
        ("not a url", False),
    ],
)
def test_static_endpoint_classification(url: str, local: bool) -> None:
    assert is_local_endpoint(url) is local


def test_resolution_must_stay_local() -> None:
    assert resolves_locally("http://ollama:11434", lambda h: ["172.18.0.2"]) == (True, "")
    ok, reason = resolves_locally("http://ollama:11434", lambda h: ["172.18.0.2", "93.184.216.34"])
    assert not ok and "93.184.216.34" in reason
    assert resolves_locally("http://1.1.1.1")[0] is False
    assert resolves_locally("http://10.0.0.1")[0] is True

    def fail(host: str) -> list[str]:
        raise OSError("no such host")

    ok, reason = resolves_locally("http://ghost.internal", fail)
    assert not ok and "does not resolve" in reason
    assert resolves_locally("http://empty.internal", lambda h: [])[0] is False
    assert resolves_locally("nonsense")[0] is False


def _config(**ai: object) -> dict[str, object]:
    return {
        "schema_version": "2.1",
        "project": {"name": "p"},
        "security": {"network": "local-only"},
        "ai": {"enabled": True, "provider": "ollama", "model": "m", **ai},
    }


def test_local_only_rejects_a_remote_base_url_for_a_local_provider() -> None:
    with pytest.raises(ValidationError, match=r"ai sends requests to http://ollama\.example"):
        AICodingConfig.model_validate(_config(base_url="http://ollama.example.com:11434/v1"))
    with pytest.raises(ValidationError, match=r"ai\.fallback\[0\]"):
        AICodingConfig.model_validate(
            _config(fallback=[{"provider": "vllm", "model": "x", "base_url": "https://vllm.io"}])
        )
    with pytest.raises(ValidationError, match=r"ai\.routing\.security"):
        AICodingConfig.model_validate(
            _config(
                routing={
                    "security": {
                        "provider": "ollama",
                        "model": "y",
                        "base_url": "http://93.184.216.34:11434",
                    }
                }
            )
        )
    ok = AICodingConfig.model_validate(_config(base_url="http://ollama:11434/v1"))
    assert [w for w, _ in ok.ai.routes_in_use()] == ["ai"]
    # Outside local-only the same endpoint is the operator's choice.
    open_doc = {**_config(base_url="http://ollama.example.com:11434/v1"),
                "security": {"network": "open"}}  # fmt: skip
    assert AICodingConfig.model_validate(open_doc).ai.base_url


class _Pass:
    """The policy gate and the budget are only handed through by build_llm."""


def test_runtime_check_catches_a_local_name_that_resolves_outside() -> None:
    cfg = AICodingConfig.model_validate(_config(base_url="http://ollama:11434/v1"))
    gate: Any = _Pass()
    budget: Any = _Pass()
    with pytest.raises(LLMUnavailableError, match="resolves to public address"):
        build_llm(cfg, AgentName.SECURITY, policy=gate, budget=budget,
                  resolver=lambda h: ["93.184.216.34"])  # fmt: skip
    llm = build_llm(cfg, AgentName.SECURITY, policy=gate, budget=budget,
                    resolver=lambda h: ["172.18.0.2"])  # fmt: skip
    assert llm is not None
    default = AICodingConfig.model_validate(_config())  # Ollama's default localhost URL
    assert build_llm(default, AgentName.SECURITY, policy=gate, budget=budget) is not None


async def test_every_request_is_checked_against_the_address_it_goes_to() -> None:
    """DNS rebinding: the name resolved to a local address when the model was set up,
    then to a public one. A check made once (and a client resolving again on its own)
    let prompts with source code go outside; each request is now checked and sent to the
    checked address."""
    import httpx  # noqa: PLC0415

    from ai_coding.security.endpoints import LocalOnlyTransport  # noqa: PLC0415

    answers = iter([["172.18.0.2"], ["93.184.216.34"], ["10.0.0.7", "8.8.8.8"]])
    sent: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = LocalOnlyTransport(httpx.MockTransport(upstream),
                                   resolver=lambda _h: next(answers))  # fmt: skip
    async with httpx.AsyncClient(transport=transport) as client:
        ok = await client.post("http://ollama:11434/api/chat", json={})
        assert ok.status_code == 200
        for _ in range(2):  # rebound to a public address; one public address among local
            with pytest.raises(httpx.ConnectError, match="public address"):
                await client.post("http://ollama:11434/api/chat", json={})
        with pytest.raises(httpx.ConnectError, match="public address"):
            await client.get("http://93.184.216.34/")
    assert len(sent) == 1
    assert sent[0].url.host == "172.18.0.2" and sent[0].headers["Host"] == "ollama:11434"


async def test_https_keeps_the_name_for_tls() -> None:
    import httpx  # noqa: PLC0415

    from ai_coding.security.endpoints import LocalOnlyTransport  # noqa: PLC0415

    sent: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    transport = LocalOnlyTransport(httpx.MockTransport(upstream),
                                   resolver=lambda _h: ["10.1.2.3"])  # fmt: skip
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://vllm.internal:8443/v1/models")
    (req,) = sent
    assert req.url.host == "10.1.2.3" and req.url.port == 8443
    assert req.extensions["sni_hostname"] == "vllm.internal"
    assert req.headers["Host"] == "vllm.internal:8443"


def test_local_only_models_get_the_checking_transport() -> None:
    from ai_coding.security.endpoints import LocalOnlyTransport  # noqa: PLC0415

    cfg = AICodingConfig.model_validate(_config(base_url="http://ollama:11434/v1"))
    gate: Any = _Pass()
    budget: Any = _Pass()
    llm = build_llm(cfg, AgentName.SECURITY, policy=gate, budget=budget,
                    resolver=lambda h: ["172.18.0.2"])  # fmt: skip
    assert all(isinstance(r.provider._transport, LocalOnlyTransport) for r in llm.routes)  # type: ignore[attr-defined]
    open_doc = {**_config(base_url="http://ollama:11434/v1"), "security": {"network": "open"}}
    other = build_llm(AICodingConfig.model_validate(open_doc), AgentName.SECURITY,
                      policy=gate, budget=budget)  # fmt: skip
    assert not any(isinstance(r.provider._transport, LocalOnlyTransport) for r in other.routes)  # type: ignore[attr-defined]
