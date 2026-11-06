"""GitHub client: auth, retries, rate limits, pagination, redaction (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_coding.domain.errors import PermanentError, TransientError
from ai_coding.integrations.github import (
    AppAuth,
    GitHubAPIError,
    GitHubClient,
    RateLimitError,
    TokenAuth,
    auth_from_env,
    parse_repo_slug,
)
from ai_coding.integrations.github.auth import GitHubAuthError
from ai_coding.security import redact

TOKEN = "ghs_" + "a" * 36


def client(
    handler: Callable[[httpx.Request], httpx.Response], **kw: object
) -> tuple[GitHubClient, list[float]]:
    sleeps: list[float] = []
    c = GitHubClient(
        TokenAuth(TOKEN), transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )  # type: ignore[arg-type]
    return c, sleeps


def test_headers_and_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    c, _ = client(handler)
    assert c.json("GET", "/repos/a/b") == {"ok": True}
    headers = seen[0].headers
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["user-agent"] == "ai-coding-platform"
    assert TOKEN not in redact(f"token {TOKEN}")  # registered as a literal secret
    c.close()


def test_204_returns_none() -> None:
    c, _ = client(lambda r: httpx.Response(204))
    assert c.json("POST", "/x") is None


def test_server_errors_are_retried_then_raised() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502 if len(calls) < 3 else 200, json={"n": len(calls)})

    c, sleeps = client(handler)
    assert c.json("GET", "/x") == {"n": 3}
    assert sleeps == [2, 4]
    failing, _ = client(lambda r: httpx.Response(503, json={"message": "down"}), max_retries=1)
    with pytest.raises(GitHubAPIError, match="503"):
        failing.json("GET", "/x")


def test_transport_errors_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c, sleeps = client(handler, max_retries=2)
    with pytest.raises(TransientError, match="unreachable"):
        c.json("GET", "/x")
    assert len(sleeps) == 2


def test_secondary_rate_limit_waits_retry_after() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                403,
                headers={"retry-after": "3"},
                json={"message": "You have exceeded a secondary rate limit"},
            )
        return httpx.Response(200, json={})

    c, sleeps = client(handler)
    c.json("GET", "/x")
    assert sleeps == [3.0]


def test_long_rate_limit_is_not_waited() -> None:
    c, sleeps = client(
        lambda r: httpx.Response(
            429, headers={"retry-after": "3600"}, json={"message": "slow down"}
        )
    )
    with pytest.raises(RateLimitError):
        c.json("GET", "/x")
    assert sleeps == []


def test_error_messages_are_redacted() -> None:
    c, _ = client(lambda r: httpx.Response(404, json={"message": f"token {TOKEN} not found"}))
    with pytest.raises(GitHubAPIError) as info:
        c.json("GET", "/x")
    assert info.value.status == 404 and TOKEN not in str(info.value)
    html, _ = client(lambda r: httpx.Response(500, text="<html>oops</html>"), max_retries=0)
    with pytest.raises(GitHubAPIError, match="Internal Server Error"):
        html.json("GET", "/x")


def test_pagination_follows_link_and_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = (
            {"link": f'<https://api.github.com/items?page={page + 1}>; rel="next"'}
            if page < 3
            else {}
        )
        return httpx.Response(200, json=[page * 10, page * 10 + 1], headers=headers)

    c, _ = client(handler)
    assert list(c.paginate("/items")) == [10, 11, 20, 21, 30, 31]
    assert list(c.paginate("/items", limit=3)) == [10, 11, 20]
    keyed, _ = client(lambda r: httpx.Response(200, json={"jobs": [1, 2]}))
    assert list(keyed.paginate("/jobs", key="jobs")) == [1, 2]


def test_https_required() -> None:
    with pytest.raises(ValueError, match="https"):
        GitHubClient(TokenAuth(TOKEN), api_url="http://evil.example.com")


@pytest.mark.parametrize(
    ("value", "slug"),
    [
        ("acme/demo", "acme/demo"),
        ("https://github.com/acme/demo.git", "acme/demo"),
        ("https://x-access-token:abc@github.com/acme/demo", "acme/demo"),
        ("git@github.com:acme/demo.git", "acme/demo"),
        ("ssh://git@github.com/acme/demo.git", "acme/demo"),
    ],
)
def test_parse_repo_slug(value: str, slug: str) -> None:
    assert parse_repo_slug(value) == slug


@pytest.mark.parametrize("value", ["", "acme", "a/b/c", "https://github.com/acme", "../x/y"])
def test_parse_repo_slug_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        parse_repo_slug(value)


# -------------------------------------------------------------------- auth --
@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def test_app_auth_exchanges_and_caches_installation_token(private_key: str) -> None:
    now = [1_790_000_000.0]  # 2026-09, before the token expiry below
    exchanges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            app_jwt = request.headers["authorization"].removeprefix("Bearer ")
            claims = jwt.decode(app_jwt, options={"verify_signature": False})
            assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600
            exchanges.append(app_jwt)
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_install{len(exchanges)}" + "x" * 30,
                    "expires_at": "2027-01-15T08:00:00Z",
                },
            )
        return httpx.Response(200, json={"auth": request.headers["authorization"]})

    auth = AppAuth("123", "456", private_key, clock=lambda: now[0])
    c = GitHubClient(auth, transport=httpx.MockTransport(handler))
    first = c.json("GET", "/x")["auth"]
    assert first.startswith("Bearer ghs_install1")
    c.json("GET", "/x")
    assert len(exchanges) == 1  # cached
    now[0] = 1_900_000_000.0  # past expiry
    assert c.json("GET", "/x")["auth"].startswith("Bearer ghs_install2")


def test_access_token_shares_the_cached_installation_token(private_key: str) -> None:
    """git push gets the token through the public access_token(); it is the same cached
    installation token the API requests use (one exchange)."""
    exchanges: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            exchanges.append(1)
            return httpx.Response(201, json={"token": "ghs_shared" + "x" * 30,
                                             "expires_at": "2027-01-15T08:00:00Z"})  # fmt: skip
        return httpx.Response(200, json={"auth": request.headers["authorization"]})

    auth = AppAuth("123", "456", private_key, clock=lambda: 1_790_000_000.0)
    c = GitHubClient(auth, transport=httpx.MockTransport(handler))
    token = c.access_token()
    assert token.startswith("ghs_shared") and not token.startswith("Bearer")
    assert c.json("GET", "/x")["auth"] == f"Bearer {token}"
    assert len(exchanges) == 1


def test_access_token_of_a_plain_token() -> None:
    c = GitHubClient(TokenAuth(TOKEN), transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert c.access_token() == TOKEN


def test_app_auth_validation(private_key: str) -> None:
    with pytest.raises(GitHubAuthError, match="numeric"):
        AppAuth("abc", "1", private_key)
    with pytest.raises(GitHubAuthError, match="PEM"):
        AppAuth("1", "2", "not a key")
    with pytest.raises(GitHubAuthError):
        TokenAuth("has space")


def test_auth_from_env(private_key: str, tmp_path: object) -> None:
    assert isinstance(auth_from_env({"GITHUB_TOKEN": TOKEN}), TokenAuth)
    assert (
        auth_from_env({"AI_CODING_GITHUB_TOKEN": "t1" * 10, "GITHUB_TOKEN": TOKEN}).token
        == "t1" * 10
    )  # type: ignore[union-attr]
    app = auth_from_env(
        {
            "AI_CODING_GITHUB_APP_ID": "1",
            "AI_CODING_GITHUB_APP_INSTALLATION_ID": "2",
            "AI_CODING_GITHUB_APP_PRIVATE_KEY": private_key,
            "GITHUB_TOKEN": TOKEN,
        }
    )
    assert isinstance(app, AppAuth)
    from pathlib import Path  # noqa: PLC0415

    key_file = Path(str(tmp_path)) / "key.pem"
    key_file.write_text(private_key, encoding="utf-8")
    assert isinstance(
        auth_from_env(
            {
                "AI_CODING_GITHUB_APP_ID": "1",
                "AI_CODING_GITHUB_APP_INSTALLATION_ID": "2",
                "AI_CODING_GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
            }
        ),
        AppAuth,
    )
    with pytest.raises(GitHubAuthError, match="no GitHub credentials"):
        auth_from_env({})
    assert json.dumps({}) == "{}"


def _download_handler(location: str, blob: bytes, seen: list[httpx.Request]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=blob)

    return handler


def test_download_follows_signed_redirect_without_token() -> None:
    seen: list[httpx.Request] = []
    c, _ = client(_download_handler("https://blob.example/x?sig=abc", b"PK-data", seen))
    assert c.download("/repos/o/r/actions/artifacts/1/zip", max_bytes=100) == b"PK-data"
    api, blob = seen
    assert api.headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in blob.headers


def test_download_rejects_plain_http_and_oversized_bodies() -> None:
    c, _ = client(_download_handler("http://blob.example/x", b"", []))
    with pytest.raises(GitHubAPIError, match="not https"):
        c.download("/a", max_bytes=10)
    c, _ = client(_download_handler("https://blob.example/x", b"0" * 11, []))
    with pytest.raises(PermanentError, match="limit"):
        c.download("/a", max_bytes=10)
