"""The CLI's connection to a server (ai_coding.cli.remote, ADR-0047): addresses, the
sign-in and link files, and how the client reports the server's answers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ai_coding.cli import remote


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in (remote.URL_ENV, remote.TOKEN_ENV, remote.PROJECT_ENV):
        monkeypatch.delenv(name, raising=False)


def test_server_addresses() -> None:
    assert remote.normalize_url(" http://10.0.1.115:8090/ ") == "http://10.0.1.115:8090"
    assert remote.normalize_url("https://ai.example/platform/") == "https://ai.example/platform"
    for bad in (
        "10.0.1.115:8090",
        "ftp://host",
        "https://user:pw@host",
        "https://host/?a=1",
        "http://",
    ):
        with pytest.raises(ValueError):
            remote.normalize_url(bad)
    assert remote.plain_http("http://10.0.1.115:8090") is True
    assert remote.plain_http("http://localhost:8090") is False
    assert remote.plain_http("https://ai.example") is False


def test_sign_in_and_link_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert remote.load_credentials() is None
    remote.save_credentials(remote.Credentials("https://ai.example", "aic_secret", "dev"))
    assert remote.load_credentials() == remote.Credentials(
        "https://ai.example", "aic_secret", "dev"
    )
    monkeypatch.setenv(remote.URL_ENV, "https://ci.example/")
    monkeypatch.setenv(remote.TOKEN_ENV, "aic_ci")
    assert remote.load_credentials() == remote.Credentials("https://ci.example", "aic_ci")  # CI
    assert remote.forget_credentials() is True and remote.forget_credentials() is False

    root = tmp_path / "project"
    root.mkdir()
    assert remote.load_link(root) is None
    remote.save_link(root, remote.Link("p-1", "alpha", "https://ai.example"))
    assert (root / ".ai-coding" / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    assert remote.load_link(root) == remote.Link("p-1", "alpha", "https://ai.example")
    monkeypatch.setenv(remote.PROJECT_ENV, "p-2")
    assert remote.load_link(root) == remote.Link("p-2")
    (root / ".ai-coding" / "server.json").write_text("[broken", encoding="utf-8")
    monkeypatch.delenv(remote.PROJECT_ENV)
    assert remote.load_link(root) is None  # unreadable: not linked, not a crash


def _client(handler: httpx.MockTransport) -> remote.Client:
    return remote.Client(remote.Credentials("https://ai.example", "aic_secret"), transport=handler)


def test_the_client_explains_refusals_without_the_token() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"username": "dev"})
        if request.url.path == "/api/v1/expired":
            return httpx.Response(
                401, json={"error": {"code": "unauthorized", "message": "bad token"}}
            )
        if request.url.path == "/api/v1/html":
            return httpx.Response(200, text="<html>router login page</html>")
        return httpx.Response(404, json={"error": {"message": "project not found"}})

    with _client(httpx.MockTransport(answer)) as client:
        assert client.call("GET", "/me") == {"username": "dev"}
        with pytest.raises(remote.RemoteError, match=r"HTTP 401 bad token.*ai-coding server login"):
            client.call("GET", "/expired")
        with pytest.raises(remote.RemoteError, match="HTTP 404 project not found"):
            client.call("POST", "/projects/x/local-runs", {"a": 1})
        with pytest.raises(remote.RemoteError, match="did not answer with JSON"):
            client.call("GET", "/html")
    assert seen[0].headers["Authorization"] == "Bearer aic_secret"
    assert seen[0].headers["User-Agent"].startswith("ai-coding-cli/")
    assert json.loads(seen[2].content) == {"a": 1}

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(httpx.MockTransport(down)) as client, pytest.raises(remote.RemoteError) as err:
        client.call("GET", "/me")
    assert "is not reachable: ConnectError" in str(err.value) and "aic_secret" not in str(err.value)
