"""B-807: S3ArtifactStore against a real S3-compatible server (VersityGW, digest-pinned)
that verifies every SigV4 signature itself — not a fake. Covers the store operations,
ListObjectsV2 pagination, authentication failures, integrity, and a server run whose
artifacts live in the bucket and are downloaded through the API."""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings
from ai_coding.api.worker import Worker
from ai_coding.persistence.artifacts import ArtifactIntegrityError
from ai_coding.persistence.s3 import S3ArtifactStore, S3Error
from tests.integration.conftest import require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]
S3_IMAGE = (
    "mirror.gcr.io/versity/versitygw"
    "@sha256:89cad13cc981986ee284cbafea4613f9533ce6fd5f37a2c96c4c4fb8adb3a316"
)  # v1.10.0
ACCESS = "aicodingtest"
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def s3() -> Iterator[tuple[str, str]]:
    """A throwaway S3 gateway; yields (endpoint, secret)."""
    require_images(S3_IMAGE)
    secret, name = secrets.token_hex(20), f"aic-s3-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::7070",
            "--tmpfs",
            "/data:rw",
            S3_IMAGE,
            "--access",
            ACCESS,
            "--secret",
            secret,
            "--port",
            ":7070",
            "posix",
            "/data",
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "7070/tcp"], check=True, capture_output=True, text=True
            )
            .stdout.split(":")[-1]
            .strip()
        )
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(endpoint, timeout=1)
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        yield endpoint, secret
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _store(
    s3: tuple[str, str],
    *,
    secret: str | None = None,
    bucket: str = "artifacts",
    prefix: str = "projects/a",
) -> S3ArtifactStore:
    endpoint, real = s3
    return S3ArtifactStore(
        endpoint=endpoint,
        bucket=bucket,
        access_key=ACCESS,
        secret_key=secret or real,
        prefix=prefix,
    )


def test_store_operations_against_a_real_server(s3: tuple[str, str]) -> None:
    store = _store(s3)
    store.ensure_bucket()
    store.ensure_bucket()  # idempotent
    refs = [store.put(f"artifact {i}".encode()) for i in range(7)]
    assert store.put(b"artifact 0").sha256 == refs[0].sha256  # content-addressed
    assert store.get(refs[3].sha256) == b"artifact 3" and store.exists(refs[3].sha256)
    keys = list(store.keys(page_size=2))  # four pages of ListObjectsV2
    assert len(keys) == 7 and all(k.startswith("projects/a/sha256/") for k in keys)
    other = _store(s3, prefix="projects/b")  # another project's prefix is separate
    assert list(other.keys()) == [] and not other.exists(refs[0].sha256)
    assert store.delete(refs[0].sha256) and not store.exists(refs[0].sha256)
    with pytest.raises(FileNotFoundError):
        store.get(refs[0].sha256)
    assert store.clear() == 6 and list(store.keys()) == []


def test_bad_credentials_are_refused_by_the_server(s3: tuple[str, str]) -> None:
    store = _store(s3)
    store.ensure_bucket()
    digest = store.put(b"secret build output").sha256
    with pytest.raises(S3Error, match="SignatureDoesNotMatch"):
        _store(s3, secret="not-the-secret-" + "x" * 20).get(digest)
    with pytest.raises(S3Error, match="403"):
        _store(s3, secret="not-the-secret-" + "x" * 20).put(b"forged")


def test_a_tampered_object_is_detected(s3: tuple[str, str]) -> None:
    store = _store(s3, prefix="projects/tamper")
    store.ensure_bucket()
    digest = store.put(b"original report").sha256
    replaced = store._request("PUT", store._key(digest), body=b"replaced in the bucket")
    assert replaced.status_code == 200
    with pytest.raises(ArtifactIntegrityError):
        store.get(digest)


def test_server_run_artifacts_live_in_the_bucket(
    s3: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint, secret = s3
    for key, value in {
        "ENDPOINT": endpoint,
        "BUCKET": "server",
        "ACCESS_KEY_ID": ACCESS,
        "SECRET_ACCESS_KEY": secret,
    }.items():
        monkeypatch.setenv(f"AI_CODING_S3_{key}", value)
    _store(s3, bucket="server").ensure_bucket()
    repo = tmp_path / "repos" / "alpha"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    config = {
        "schema_version": "2.1",
        "project": {"name": "alpha"},
        "artifacts": {"store": "minio"},
    }
    (repo / ".ai-coding.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    srv = Server(
        ServerSettings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 's.db'}",
            allowed_roots=(repo.parent.resolve(),),
        )
    )
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    admin = srv.login("admin", PASSWORD)
    assert admin is not None
    project = srv.create_project(admin, "alpha", path=str(repo.resolve()))
    run = srv.create_run(admin, project, "discover", {})
    assert asyncio.run(Worker(srv, poll_seconds=0.01).run_until_idle(timeout=120)) == 1
    assert srv.run_status(admin, run)["state"] == "COMPLETED"
    art = srv.artifacts(admin, run)[0]
    bucket = _store(s3, bucket="server", prefix="projects/alpha")
    assert any(k.endswith(art["sha256"]) for k in bucket.keys(page_size=100))  # stored in S3
    assert not (repo / ".ai-coding" / "artifacts").exists()  # not on the local disk
    token, _ = srv.issue_token(admin, "t")
    with TestClient(create_app(srv)) as c:
        r = c.get(
            f"/api/v1/runs/{run}/artifacts/{art['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200 and r.content == bucket.get(art["sha256"])
    srv.close()
