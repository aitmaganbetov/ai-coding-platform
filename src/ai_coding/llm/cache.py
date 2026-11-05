"""Response cache keyed by the *redacted* request (ТЗ §18 "стоимость"; ADR-0023).

Only deterministic requests (``temperature == 0``) are cached. The key covers the
provider, endpoint, model, every redacted message, the output schema and the token
limit, so a different prompt, model or schema never reuses an answer. Because the key
and the stored text are computed after redaction, secrets never reach the cache.
Cached text is re-validated against the schema on every hit, like a fresh answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.llm.base import LLMRequest, LLMResponse, TokenUsage

CACHE_FORMAT = 1


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """What is stored: text and the model that produced it (no usage is re-billed)."""

    text: str
    model: str


def cache_key(provider: str, base_url: str | None, request: LLMRequest) -> str:
    """SHA-256 over everything that influences the answer."""
    doc: dict[str, Any] = {
        "format": CACHE_FORMAT,
        "provider": provider,
        "base_url": base_url or "",
        "model": request.model,
        "messages": [[m.role, m.content, m.untrusted] for m in request.messages],
        "schema": request.response_schema,
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
    }
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


class ResponseCache:
    """File-backed cache: ``<dir>/<aa>/<key>.json``, written atomically."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def applies(request: LLMRequest) -> bool:
        """Only deterministic requests are cacheable."""
        return request.temperature == 0

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> CachedResponse | None:
        """Cached response or ``None`` (a corrupt entry counts as a miss)."""
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
            if data.get("format") != CACHE_FORMAT:
                return None
            return CachedResponse(text=str(data["text"]), model=str(data["model"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, response: LLMResponse) -> None:
        """Store ``response`` (already redacted by the guard)."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"format": CACHE_FORMAT, "text": response.text, "model": response.model},
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            Path(tmp).replace(path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def as_response(entry: CachedResponse) -> LLMResponse:
        """A cache hit costs nothing and uses no tokens."""
        return LLMResponse(text=entry.text, model=entry.model, usage=TokenUsage(), cost_usd=0.0)
