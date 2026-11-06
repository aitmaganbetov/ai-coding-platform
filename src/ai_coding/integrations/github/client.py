"""Minimal, well-behaved GitHub REST client (httpx).

* retries 5xx and secondary rate limits with bounded backoff (``Retry-After``);
* refuses to sleep longer than ``max_wait_seconds`` on primary rate limits;
* follows ``Link: rel="next"`` pagination;
* errors never include credentials (redacted) or full response bodies.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NoReturn

import httpx

from ai_coding.domain.errors import PermanentError, TransientError
from ai_coding.integrations.github.auth import GitHubAuth
from ai_coding.security import redact

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?(?P<h1>[^/]+)/|git@(?P<h2>[^:]+):|ssh://git@(?P<h3>[^/]+)/)"
    r"(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$"
)
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubAPIError(PermanentError):
    """GitHub returned an error the caller must handle."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {redact(message)[:500]}")
        self.status = status


class RateLimitError(TransientError):
    """Rate limited for longer than we are willing to wait."""


def parse_repo_slug(value: str) -> str:
    """``owner/repo`` from a slug or a GitHub remote URL."""
    value = value.strip()
    if _SLUG.match(value):
        return value
    match = _REMOTE.match(value)
    if match and _SLUG.match(match.group("slug")):
        return match.group("slug")
    raise ValueError(f"not a GitHub repository: {redact(value)!r}")


class GitHubClient:
    """Synchronous REST client; thread-safe for concurrent requests."""

    def __init__(
        self,
        auth: GitHubAuth,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_wait_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_url.startswith("https://") and not api_url.startswith("http://localhost"):
            raise ValueError("GitHub API URL must use https")
        self._auth = auth
        self._max_retries = max_retries
        self._max_wait = max_wait_seconds
        self._sleep = sleep
        self._transport = transport
        self._timeout = timeout
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "ai-coding-platform",
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def access_token(self) -> str:
        """The token this client authenticates with (an App's installation token is
        exchanged and cached by its auth) — for ``git push`` over HTTPS."""
        return self._auth.authorization(self._exchange).removeprefix("Bearer ")

    def _exchange(self, path: str, app_jwt: str) -> Mapping[str, Any]:
        response = self._http.post(path, headers={"Authorization": f"Bearer {app_jwt}"})
        if response.status_code >= 400:
            raise GitHubAPIError(response.status_code, _message(response))
        data: Mapping[str, Any] = response.json()
        return data

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        """Perform a request with auth, retries and rate-limit handling."""
        attempt = 0
        while True:
            headers = {"Authorization": self._auth.authorization(self._exchange)}
            if accept:
                headers["Accept"] = accept
            try:
                response = self._http.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise TransientError(f"GitHub unreachable: {type(exc).__name__}") from exc
                attempt += 1
                self._sleep(min(2**attempt, self._max_wait))
                continue
            wait = self._retry_after(response)
            if wait is not None and attempt < self._max_retries:
                attempt += 1
                self._sleep(wait)
                continue
            if response.status_code in {403, 429} and _is_rate_limited(response):
                raise RateLimitError("GitHub rate limit exceeded; retry later")
            if response.status_code >= 500 and attempt < self._max_retries:
                attempt += 1
                self._sleep(min(2**attempt, self._max_wait))
                continue
            if response.status_code >= 400:
                raise GitHubAPIError(response.status_code, _message(response))
            return response

    def _retry_after(self, response: httpx.Response) -> float | None:
        if response.status_code not in {403, 429} or not _is_rate_limited(response):
            return None
        if "retry-after" in response.headers:
            wait = float(response.headers["retry-after"])
        elif response.headers.get("x-ratelimit-remaining") == "0":
            reset = float(response.headers.get("x-ratelimit-reset", "0"))
            wait = max(0.0, reset - time.time()) + 1
        else:
            wait = 60.0
        return wait if wait <= self._max_wait else None

    def download(self, path: str, *, max_bytes: int) -> bytes:
        """Fetch a binary that GitHub serves via a redirect to signed blob storage.

        The API answers ``302`` with a short-lived signed URL. That URL is followed
        with a *separate* client that sends no ``Authorization`` header (the signature
        is the credential), must be https, and the body is capped at ``max_bytes``.
        """
        response = self.request("GET", path)
        if response.status_code not in {301, 302, 303, 307, 308}:
            if len(response.content) > max_bytes:
                _too_large(max_bytes)
            return response.content
        location = response.headers.get("location", "")
        if not location.startswith("https://") and not location.startswith("http://localhost"):
            raise GitHubAPIError(response.status_code, "download redirect is not https")
        chunks: list[bytes] = []
        size = 0
        with (
            httpx.Client(
                transport=self._transport, timeout=self._timeout, follow_redirects=False
            ) as blob,
            blob.stream("GET", location, headers={"User-Agent": "ai-coding-platform"}) as stream,
        ):
            if stream.status_code >= 400:
                raise GitHubAPIError(stream.status_code, "artifact download failed")
            for chunk in stream.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    _too_large(max_bytes)
                chunks.append(chunk)
        return b"".join(chunks)

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Request and decode JSON (``None`` for 204)."""
        response = self.request(method, path, **kwargs)
        return None if response.status_code == 204 or not response.content else response.json()

    def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        key: str | None = None,
        limit: int = 1000,
    ) -> Iterator[Any]:
        """Iterate items across pages (``key`` selects the list in object responses)."""
        query = {"per_page": 100, **(params or {})}
        url: str | None = path
        count = 0
        while url:
            response = self.request("GET", url, params=query if url == path else None)
            data = response.json()
            items = data[key] if key else data
            for item in items:
                yield item
                count += 1
                if count >= limit:
                    return
            match = _LINK_NEXT.search(response.headers.get("link", ""))
            url = match.group(1) if match else None


def _too_large(max_bytes: int) -> NoReturn:
    raise PermanentError(f"download exceeds the {max_bytes}-byte limit")


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in _message(response).lower()


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.reason_phrase
    if isinstance(data, dict):
        return str(data.get("message", response.reason_phrase))
    return response.reason_phrase
