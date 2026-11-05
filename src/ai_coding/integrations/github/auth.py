"""GitHub authentication: fine-grained/Actions token or GitHub App installation token.

Credentials come only from the environment (ТЗ §11: never from YAML):

* ``AI_CODING_GITHUB_TOKEN`` or ``GITHUB_TOKEN`` — a token (Actions token, fine-grained PAT);
* ``AI_CODING_GITHUB_APP_ID`` + ``AI_CODING_GITHUB_APP_INSTALLATION_ID`` +
  ``AI_CODING_GITHUB_APP_PRIVATE_KEY`` (PEM) or ``…_PRIVATE_KEY_FILE`` — a GitHub App.

App installation tokens are short-lived (1 h) and are refreshed automatically.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import jwt

from ai_coding.security import default_redactor

_REFRESH_MARGIN_SECONDS = 120


class GitHubAuthError(Exception):
    """Credentials are missing or invalid."""


class GitHubAuth(Protocol):
    """Produces the ``Authorization`` header value."""

    def authorization(self, exchange: Callable[[str, str], Mapping[str, Any]]) -> str:
        """Header value; ``exchange(path, app_jwt)`` lets an App obtain an installation token."""
        ...


@dataclass(frozen=True)
class TokenAuth:
    """A plain token (GITHUB_TOKEN in Actions or a fine-grained PAT)."""

    token: str

    def __post_init__(self) -> None:
        if not self.token or any(c.isspace() for c in self.token):
            raise GitHubAuthError("GitHub token is empty or malformed")
        default_redactor.add_literals([self.token])

    def authorization(self, exchange: Callable[[str, str], Mapping[str, Any]]) -> str:
        return f"Bearer {self.token}"


@dataclass
class AppAuth:
    """GitHub App: RS256 JWT → installation access token (cached until near expiry)."""

    app_id: str
    installation_id: str
    private_key: str
    clock: Callable[[], float] = time.time
    _token: str | None = field(default=None, repr=False)
    _expires_at: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.app_id.isdigit() or not self.installation_id.isdigit():
            raise GitHubAuthError("GitHub App id and installation id must be numeric")
        if "PRIVATE KEY" not in self.private_key:
            raise GitHubAuthError("GitHub App private key must be a PEM document")

    def app_jwt(self) -> str:
        """Short-lived JWT identifying the App (GitHub allows at most 10 minutes)."""
        now = int(self.clock())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def authorization(self, exchange: Callable[[str, str], Mapping[str, Any]]) -> str:
        with self._lock:
            if self._token is None or self.clock() >= self._expires_at - _REFRESH_MARGIN_SECONDS:
                data = exchange(
                    f"/app/installations/{self.installation_id}/access_tokens", self.app_jwt()
                )
                token = str(data["token"])
                default_redactor.add_literals([token])
                self._token = token
                self._expires_at = _parse_expiry(str(data.get("expires_at", "")), self.clock())
            return f"Bearer {self._token}"


def _parse_expiry(value: str, now: float) -> float:
    from datetime import datetime  # noqa: PLC0415

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return now + 3000


def auth_from_env(env: Mapping[str, str] | None = None) -> GitHubAuth:
    """Build credentials from the environment (App takes precedence over a token)."""
    env = os.environ if env is None else env
    app_id = env.get("AI_CODING_GITHUB_APP_ID")
    if app_id:
        installation = env.get("AI_CODING_GITHUB_APP_INSTALLATION_ID", "")
        key = env.get("AI_CODING_GITHUB_APP_PRIVATE_KEY", "")
        key_file = env.get("AI_CODING_GITHUB_APP_PRIVATE_KEY_FILE")
        if not key and key_file:
            key = Path(key_file).read_text(encoding="utf-8")
        return AppAuth(app_id=app_id, installation_id=installation, private_key=key)
    token = env.get("AI_CODING_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        return TokenAuth(token.strip())
    raise GitHubAuthError(
        "no GitHub credentials: set AI_CODING_GITHUB_TOKEN/GITHUB_TOKEN or the "
        "AI_CODING_GITHUB_APP_* variables"
    )
