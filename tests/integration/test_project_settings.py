"""Settings and encrypted secrets of projects (ADR-0043, ADR-0046, ADR-0048): write-only
secrets, bound to their project and name, under the server's key; sections with a version guard."""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.core import ConflictError
from ai_coding.api.project_settings import SecretStoreUnavailableError, key_id, parse_key
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import AuditEvent, ProjectSecret

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
KEY = base64.b64encode(os.urandom(32)).decode()
TOKEN = "ghp_SuperSecretValue1234567890abcdef"


def make_server(tmp_path: Path, key: str | None) -> Server:
    roots = tmp_path / "repos"
    for name in ("alpha", "beta"):
        (roots / name).mkdir(parents=True, exist_ok=True)
    db = f"sqlite:///{tmp_path / 's.db'}"
    srv = Server(ServerSettings(data_dir=tmp_path / "data", database_url=db,
                                allowed_roots=(roots.resolve(),), secret_key=key))  # fmt: skip
    srv.migrate()
    return srv


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    srv = make_server(tmp_path, KEY)
    for name in ("owner", "maint", "dev", "outsider"):
        srv.create_user(name, PASSWORD)
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    alpha = srv.create_project(owner, "alpha", path=str(srv.settings.allowed_roots[0] / "alpha"))
    beta = srv.create_project(owner, "beta", path=str(srv.settings.allowed_roots[0] / "beta"))
    srv.set_member(owner, alpha, "maint", Role.MAINTAINER)
    srv.set_member(owner, alpha, "dev", Role.DEVELOPER)
    with TestClient(create_app(srv, broker=None)) as client:
        heads = {}
        for name in ("owner", "maint", "dev", "outsider"):
            token = client.post(
                "/api/v1/auth/login", json={"username": name, "password": PASSWORD}
            ).json()["token"]
            heads[name] = {"Authorization": f"Bearer {token}"}
        yield {"c": client, "srv": srv, "alpha": alpha, "beta": beta, "h": heads, "owner": owner}
    srv.close()


def test_secrets_are_write_only_and_encrypted(world: dict[str, Any]) -> None:
    c, srv, alpha, h = world["c"], world["srv"], world["alpha"], world["h"]
    url = f"/api/v1/projects/{alpha}/secrets"
    stored = c.put(f"{url}/github.token", json={"value": TOKEN + "\n"}, headers=h["maint"])
    assert stored.status_code == 200, stored.text
    assert stored.json()["hint"] == "…cdef" and TOKEN not in stored.text
    listed = c.get(url, headers=h["owner"])
    assert listed.json()["available"] is True
    assert [s["name"] for s in listed.json()["secrets"]] == [
        "github.token"
    ] and TOKEN not in listed.text
    with srv.session() as s:
        row = s.get(ProjectSecret, (alpha, "github.token"))
        assert row is not None and TOKEN.encode() not in row.ciphertext
        assert row.key_id == key_id(parse_key(KEY) or b"")
        actions = [
            (e.action, e.target) for e in s.query(AuditEvent).filter(AuditEvent.project_id == alpha)
        ]
    assert ("project.secret.set", "github.token") in actions
    assert srv.secret_value(alpha, "github.token") == TOKEN  # the trailing newline is dropped
    assert srv.secret_value(alpha, "missing") is None

    # Bound to its project and name: a row copied elsewhere does not decrypt.
    beta = world["beta"]
    with srv.session() as s:
        s.add(ProjectSecret(project_id=beta, name="github.token", key_id=row.key_id,
                            nonce=row.nonce, ciphertext=row.ciphertext, hint=row.hint,
                            updated_by="x"))  # fmt: skip
    with pytest.raises(SecretStoreUnavailableError, match="cannot be decrypted"):
        srv.secret_value(beta, "github.token")

    assert c.put(f"{url}/Bad Name", json={"value": "x"}, headers=h["owner"]).status_code == 422
    assert c.delete(f"{url}/github.token", headers=h["owner"]).status_code == 204
    assert c.delete(f"{url}/github.token", headers=h["owner"]).status_code == 404


def test_only_owners_and_maintainers_manage_secrets(world: dict[str, Any]) -> None:
    c, alpha, h = world["c"], world["alpha"], world["h"]
    url = f"/api/v1/projects/{alpha}/secrets"
    assert c.get(url, headers=h["dev"]).status_code == 403
    assert c.put(f"{url}/x", json={"value": "v"}, headers=h["dev"]).status_code == 403
    assert c.get(url, headers=h["outsider"]).status_code in (403, 404)
    assert c.put(f"{url}/x", json={"value": "v"}, headers=h["outsider"]).status_code in (403, 404)


def test_without_a_server_key_nothing_is_stored(tmp_path: Path) -> None:
    srv = make_server(tmp_path, None)
    srv.create_user("owner", PASSWORD)
    owner = srv.login("owner", PASSWORD)
    assert owner is not None
    pid = srv.create_project(owner, "alpha", path=str(srv.settings.allowed_roots[0] / "alpha"))
    assert srv.list_secrets(owner, pid)["available"] is False
    with pytest.raises(SecretStoreUnavailableError, match="AI_CODING_SERVER_SECRET_KEY"):
        srv.set_secret(owner, pid, "ai.key", "sk-123")
    srv.close()

    # Another key later: the stored secret is reported, not mis-decrypted.
    srv = make_server(tmp_path, KEY)
    srv.set_secret(owner, pid, "ai.key", "".join(("sk-", "1234567890")))
    srv.settings = replace(srv.settings, secret_key=base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(SecretStoreUnavailableError, match="another server key"):
        srv.secret_value(pid, "ai.key")
    srv.close()
    assert parse_key("not a key") is None and parse_key("ab" * 32) == bytes.fromhex("ab" * 32)


def test_sections_and_their_version(world: dict[str, Any]) -> None:
    srv, alpha, owner = world["srv"], world["alpha"], world["owner"]
    assert srv.read_section(owner, alpha, "github")["version"] == 0
    first = srv.write_section(owner, alpha, "github", {"repository": "org/app"})
    assert first["version"] == 1 and srv.section(alpha, "github") == {"repository": "org/app"}
    with pytest.raises(ConflictError, match="changed meanwhile"):
        srv.write_section(owner, alpha, "github", {"repository": "org/other"}, version=0)
    assert (
        srv.write_section(owner, alpha, "github", {"repository": "org/new"}, version=1)["version"]
        == 2
    )
    assert srv.section(uuid.uuid4(), "github") == {}
