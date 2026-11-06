"""Tests for redaction and secret detection."""

from __future__ import annotations

import json

import pytest

from ai_coding.security import (
    REDACTED,
    Redactor,
    find_secrets_in_mapping,
    is_secret_key,
    is_secret_reference,
    redact,
)

SAMPLES = {
    "anthropic": "sk-ant-api03-" + "x" * 40,
    "openai": "sk-proj-" + "Y" * 40,
    "github": "ghp_" + "z" * 36,
    "github_pat": "github_pat_" + "Q" * 30,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "slack": "xoxb-1234567890-abcdef",
    "google": "AIza" + "a" * 35,
    "jwt": ".".join(["eyJ" + "h" * 16, "eyJ" + "p" * 16, "s" * 20]),
}


@pytest.mark.parametrize("secret", list(SAMPLES.values()), ids=list(SAMPLES))
def test_known_token_formats_are_redacted(secret: str) -> None:
    text = f"before {secret} after"
    out = redact(text)
    assert secret not in out
    assert REDACTED in out
    assert out.startswith("before ")


def test_authorization_headers_redacted() -> None:
    token = "abcdefghijklmnopqrstuvwxyz123456"
    assert token not in redact(f'curl -H "Authorization: Bearer {token}" https://api')
    assert "dXNlcjpwYXNzd29yZA" not in redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert token not in redact(f"bearer {token}")
    assert redact("the bearer of bad news") == "the bearer of bad news"


def test_private_key_block_redacted() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert redact(f"key:\n{pem}\n") == f"key:\n{REDACTED}\n"


def test_url_credentials_redacted_keep_user() -> None:
    out = redact("postgresql://app:s3cr3tpass@db:5432/app")
    assert "s3cr3tpass" not in out
    assert out == f"postgresql://app:{REDACTED}@db:5432/app"


def test_assignment_redacted_keeps_key() -> None:
    assert redact("password=hunter2hunter2") == f"password={REDACTED}"
    value = "abc" + "def" * 3
    assert redact(f'API_KEY: "{value}"') == f'API_KEY: "{REDACTED}"'


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        # osv-scanner, GHSA-5xrq-8626-4rwp: the backslash of `\"` was redacted with the
        # token, the quote ended the string ("Expecting ',' delimiter") and the security
        # gate failed on an unparsable tool output.
        ('curl "http://localhost:51204/a?path=x&amp;token=$TOKEN" (TOKEN is the API token)',
         "$TOKEN"),
        ('open "https://host/file?sig=abcdefgh"', "abcdefgh"),
        ("secret=abcdef\\", "abcdef"),  # escaped backslash at the end: redacted with it
    ],
)  # fmt: skip
def test_redaction_keeps_json_escapes_intact(text: str, secret: str) -> None:
    out = redact(json.dumps({"details": text, "id": "GHSA-5xrq-8626-4rwp"}, indent=2))
    data = json.loads(out)
    assert data["id"] == "GHSA-5xrq-8626-4rwp"
    assert REDACTED in data["details"]
    assert secret not in data["details"]


def test_plain_text_untouched() -> None:
    text = "run pytest -q --maxfail=1 on branch feature/x"
    assert redact(text) == text


def test_literal_registration() -> None:
    r = Redactor()
    r.add_literals(["value-from-env-123", "", "abc"])
    assert r.redact("x value-from-env-123 y") == f"x {REDACTED} y"
    assert "literal" in r.find("value-from-env-123")
    assert r.redact("abc") == "abc"  # too short to register


def test_find_reports_pattern_names() -> None:
    assert "github-token" in Redactor().find(SAMPLES["github"])
    assert Redactor().find("nothing here") == []


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("api_key", True),
        ("apikey", True),
        ("token", True),
        ("github_token", True),
        ("password", True),
        ("client_secret", True),
        ("api_key_env", False),
        ("max_tokens_per_run", False),
        ("block_new_secrets", False),
        ("secret_scan", False),
        ("name", False),
    ],
)
def test_is_secret_key(key: str, expected: bool) -> None:
    assert is_secret_key(key) is expected


def test_is_secret_reference() -> None:
    assert is_secret_reference("${env:OPENAI_API_KEY}")
    assert is_secret_reference("${secret:prod/db}")
    assert not is_secret_reference("plain")


def test_find_secrets_in_nested_mapping() -> None:
    doc = {
        "ai": {"api_key": "literal", "api_key_env": "OPENAI_API_KEY"},
        "list": [{"token": "${env:T}"}, "ghp_" + "c" * 36],
        "flags": {"token": True},
        "empty": {"password": ""},
        "bad_env": {"api_key_env": SAMPLES["openai"]},
        "n": 5,
    }
    findings = {f.path: f.reason for f in find_secrets_in_mapping(doc)}
    assert set(findings) == {"ai.api_key", "list[1]", "bad_env.api_key_env"}


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/blob?se=2026&sig=K3T%2FcOtTEPUWtXlmKBSM9N&sp=r",
        "https://s3.amazonaws.com/b/k?X-Amz-Signature=abcdef0123456789&X-Amz-Date=1",
        "https://api.example.com/x?" + "access_token" + "=abcdef123456",  # built: no literal
        "https://blob.example.com/x?hmac=b238a28c9cc066451142c0ed565f",
    ],
)
def test_signed_url_parameters_redacted(url: str) -> None:
    out = redact(url)
    assert REDACTED in out
    for secret in ("K3T%2FcOtTEPUWtXlmKBSM9N", "abcdef0123456789", "b238a28c9cc066451142c0ed565f",
                   "abcdef123456"):  # fmt: skip
        assert secret not in out
    assert out.split("?")[0] == url.split("?", maxsplit=1)[0]  # host/path stay readable
