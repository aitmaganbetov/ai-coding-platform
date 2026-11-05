"""LLM provider adapters over plain HTTP (ТЗ §4; ADR-0009, ADR-0023).

* :class:`OpenAIProvider` — OpenAI Chat Completions API;
* :class:`OllamaProvider`, :class:`VLLMProvider` — the same, OpenAI-compatible wire
  format served by a local or self-hosted model (offline operation, FR-12, AC-05);
* :class:`AnthropicProvider` — Anthropic Messages API.

All adapters share one HTTP stack (httpx) so that egress, timeouts, error
classification and redaction behave identically. Structured output is requested
natively: ``response_format: json_schema`` for OpenAI-compatible servers and a forced
tool call (``tool_choice``) for Anthropic. Whatever the server returns is validated
again by :class:`~ai_coding.llm.guard.GuardedLLM` — the adapters never decide that an
answer is valid.

Error classification: network failures, timeouts, 408/409/429 and 5xx raise
:class:`ProviderUnavailableError` (transient — retry or fall back); everything else
raises :class:`ProviderRequestError` (permanent for this provider). Truncated output
raises :class:`TruncatedOutputError`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ai_coding.domain.enums import ProviderName
from ai_coding.domain.errors import PermanentError, TransientError
from ai_coding.llm.base import LLMRequest, LLMResponse, Message, TokenUsage
from ai_coding.security import default_redactor, redact

OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
ANTHROPIC_VERSION = "2023-06-01"
RESULT_TOOL = "submit_result"
_MAX_ERROR_CHARS = 500

# USD per million tokens: (input, output)
Pricing = Mapping[str, tuple[float, float]]


class ProviderUnavailableError(TransientError):
    """The provider could not answer now (network, rate limit, overload)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderRequestError(PermanentError):
    """The provider rejected the request (auth, bad request, unknown model, refusal)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TruncatedOutputError(PermanentError):
    """The model stopped at ``max_output_tokens``; the answer is incomplete."""


def _is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


class _HTTPProvider:
    """Shared HTTP plumbing; subclasses build the payload and parse the reply."""

    name: ProviderName
    default_base_url: str | None = None
    requires_https = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        pricing: Pricing | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = (base_url or self.default_base_url or "").rstrip("/")
        if not url:
            raise ValueError(f"provider '{self.name}' needs a base_url")
        scheme = urlsplit(url).scheme
        if scheme not in {"http", "https"}:
            raise ValueError(f"unsupported base_url scheme for '{self.name}': {scheme!r}")
        if scheme == "http" and self.requires_https and not _is_loopback(url):
            raise ValueError(f"provider '{self.name}' must use https")
        self.url = url
        self.base_url: str | None = url
        self._api_key = api_key
        if api_key:
            default_redactor.add_literals([api_key])
        self._timeout = timeout
        self._pricing = dict(pricing or {})
        self._transport = transport

    # ----------------------------------------------------------------- hooks --
    def _headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _endpoint(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _payload(self, request: LLMRequest) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, data: Mapping[str, Any], request: LLMRequest) -> tuple[str, str, TokenUsage]:
        raise NotImplementedError  # pragma: no cover

    # ------------------------------------------------------------------ call --
    def cost(self, model: str, usage: TokenUsage) -> float:
        """Cost in USD from the configured price table (0 when the model is unpriced)."""
        price = self._pricing.get(model)
        if price is None:
            return 0.0
        return round(usage.input_tokens * price[0] / 1e6 + usage.output_tokens * price[1] / 1e6, 6)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """One completion; raises a classified error on failure."""
        headers = {"Content-Type": "application/json", "User-Agent": "ai-coding-platform"}
        headers.update(self._headers())
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, follow_redirects=False
            ) as client:
                response = await client.post(
                    self.url + self._endpoint(),
                    headers=headers,
                    content=json.dumps(self._payload(request)),
                )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(f"{self.name}: request timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(
                f"{self.name}: unreachable ({type(exc).__name__})"
            ) from exc
        if response.status_code >= 400:
            raise self._http_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderRequestError(f"{self.name}: response is not JSON") from exc
        text, model, usage = self._parse(data, request)
        return LLMResponse(text=text, model=model, usage=usage, cost_usd=self.cost(model, usage))

    def _http_error(self, response: httpx.Response) -> Exception:
        status = response.status_code
        try:
            body = response.json()
            err = body.get("error", body) if isinstance(body, dict) else body
            message = err.get("message", "") if isinstance(err, dict) else str(err)
        except ValueError:
            message = response.text
        message = redact(str(message))[:_MAX_ERROR_CHARS]
        text = f"{self.name}: HTTP {status}: {message}".rstrip(": ")
        if status in {408, 409, 429} or status >= 500:
            retry_after: float | None = None
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            return ProviderUnavailableError(text, retry_after=retry_after)
        return ProviderRequestError(text, status=status)


# ------------------------------------------------------------ OpenAI family --
class _OpenAICompatible(_HTTPProvider):
    """Chat Completions wire format (OpenAI, Ollama ``/v1``, vLLM)."""

    max_tokens_field = "max_tokens"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _endpoint(self) -> str:
        return "/chat/completions"

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            self.max_tokens_field: request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "schema": dict(request.response_schema),
                    "strict": False,
                },
            }
        return payload

    def _parse(self, data: Mapping[str, Any], request: LLMRequest) -> tuple[str, str, TokenUsage]:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderRequestError(f"{self.name}: response has no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise ProviderRequestError(
                f"{self.name}: model refused: {redact(str(message['refusal']))[:200]}"
            )
        if choice.get("finish_reason") == "length":
            raise TruncatedOutputError(
                f"{self.name}: output truncated at max_output_tokens={request.max_output_tokens}"
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderRequestError(f"{self.name}: response has no text content")
        usage = data.get("usage") or {}
        return (
            content,
            str(data.get("model") or request.model),
            TokenUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )


class OpenAIProvider(_OpenAICompatible):
    """OpenAI API."""

    name = ProviderName.OPENAI
    default_base_url = OPENAI_BASE_URL
    max_tokens_field = "max_completion_tokens"


class OllamaProvider(_OpenAICompatible):
    """Local Ollama server (OpenAI-compatible endpoint); no API key, no egress."""

    name = ProviderName.OLLAMA
    default_base_url = OLLAMA_BASE_URL
    requires_https = False


class VLLMProvider(_OpenAICompatible):
    """Self-hosted vLLM server (OpenAI-compatible endpoint)."""

    name = ProviderName.VLLM
    requires_https = False


# ----------------------------------------------------------------- Anthropic --
class AnthropicProvider(_HTTPProvider):
    """Anthropic Messages API; structured output via a forced tool call."""

    name = ProviderName.ANTHROPIC
    default_base_url = ANTHROPIC_BASE_URL

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _endpoint(self) -> str:
        return "/v1/messages"

    @staticmethod
    def _split(messages: tuple[Message, ...]) -> tuple[str, list[dict[str, str]]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        return system, turns

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        system, turns = self._split(request.messages)
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": turns,
        }
        if system:
            payload["system"] = system
        if request.response_schema is not None:
            payload["tools"] = [
                {
                    "name": RESULT_TOOL,
                    "description": "Submit the final result. It must satisfy the schema.",
                    "input_schema": dict(request.response_schema),
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": RESULT_TOOL}
        return payload

    def _parse(self, data: Mapping[str, Any], request: LLMRequest) -> tuple[str, str, TokenUsage]:
        if data.get("stop_reason") == "max_tokens":
            raise TruncatedOutputError(
                f"{self.name}: output truncated at max_output_tokens={request.max_output_tokens}"
            )
        if data.get("stop_reason") == "refusal":
            raise ProviderRequestError(f"{self.name}: model refused the request")
        blocks = data.get("content") or []
        text: str | None = None
        if request.response_schema is not None:
            for block in blocks:
                if block.get("type") == "tool_use" and block.get("name") == RESULT_TOOL:
                    text = json.dumps(block.get("input"), ensure_ascii=False)
                    break
        if text is None:
            parts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
            if not parts:
                raise ProviderRequestError(f"{self.name}: response has no content")
            text = "".join(parts)
        usage = data.get("usage") or {}
        return (
            text,
            str(data.get("model") or request.model),
            TokenUsage(
                input_tokens=int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            ),
        )


PROVIDER_CLASSES: dict[ProviderName, type[_HTTPProvider]] = {
    ProviderName.OPENAI: OpenAIProvider,
    ProviderName.ANTHROPIC: AnthropicProvider,
    ProviderName.OLLAMA: OllamaProvider,
    ProviderName.VLLM: VLLMProvider,
}


def make_provider(
    provider: ProviderName,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
    pricing: Pricing | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> _HTTPProvider:
    """Instantiate the adapter for ``provider``."""
    cls = PROVIDER_CLASSES.get(provider)
    if cls is None:
        raise ValueError(f"no adapter for provider '{provider}' (AI disabled?)")
    return cls(
        base_url=base_url, api_key=api_key, timeout=timeout, pricing=pricing, transport=transport
    )
