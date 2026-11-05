"""S3-compatible artifact store (S8, B-705): SigV4 against AWS's published example and
the store API against an in-memory S3 that verifies every request's signature."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl

import httpx
import pytest

from ai_coding.config.generator import render_initial_config
from ai_coding.orchestrator.engine import make_artifact_store
from ai_coding.persistence.artifacts import ArtifactIntegrityError, LocalArtifactStore
from ai_coding.persistence.s3 import S3ArtifactStore, S3Error, sigv4_headers

ACCESS, SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_sigv4_matches_the_aws_documentation_example() -> None:
    """'GET Bucket (List Objects)' example of the AWS SigV4 documentation for S3."""
    headers, query = sigv4_headers(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/",
        {"max-keys": "2", "prefix": "J"},
        payload_sha256=hashlib.sha256(b"").hexdigest(),
        now=datetime(2013, 5, 24, tzinfo=UTC),
        access_key=ACCESS,
        secret_key=SECRET,
        region="us-east-1",
    )
    assert query == "max-keys=2&prefix=J"
    assert headers["authorization"].endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


class FakeS3:
    """Minimal path-style S3 that rejects requests with a wrong signature."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.buckets: set[str] = set()
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - a fake server
        self.requests.append(request)
        auth = request.headers["authorization"]
        date = datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC
        )
        query = dict(parse_qsl(request.url.query.decode()))
        expected, _ = sigv4_headers(
            request.method,
            request.headers["host"],
            request.url.raw_path.split(b"?")[0].decode(),
            query,
            payload_sha256=request.headers["x-amz-content-sha256"],
            now=date,
            access_key=ACCESS,
            secret_key=SECRET,
            region="us-east-1",
        )
        if expected["authorization"] != auth:
            return httpx.Response(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")
        if (
            request.content
            and hashlib.sha256(request.content).hexdigest()
            != request.headers["x-amz-content-sha256"]
        ):
            return httpx.Response(400, text="<Error><Code>XAmzContentSHA256Mismatch</Code></Error>")
        parts = request.url.path.lstrip("/").split("/", 1)
        bucket, key = parts[0], (parts[1] if len(parts) > 1 else "")
        if not key:
            return self._bucket(request, bucket, query)
        if request.method == "PUT":
            self.objects[key] = request.content
            return httpx.Response(200)
        if key not in self.objects:
            return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
        if request.method == "DELETE":
            del self.objects[key]
            return httpx.Response(204)
        return httpx.Response(200, content=b"" if request.method == "HEAD" else self.objects[key])

    def _bucket(self, request: httpx.Request, bucket: str, query: dict[str, str]) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200 if bucket in self.buckets else 404)
        if request.method == "PUT":
            self.buckets.add(bucket)
            return httpx.Response(200)
        keys = sorted(k for k in self.objects if k.startswith(query.get("prefix", "")))
        body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
        return httpx.Response(
            200,
            text=(
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"{body}<IsTruncated>false</IsTruncated></ListBucketResult>"
            ),
        )


def store(fake: FakeS3, prefix: str = "projects/alpha", secret: str = SECRET) -> S3ArtifactStore:
    return S3ArtifactStore(
        endpoint="http://minio.local:9000",
        bucket="artifacts",
        access_key=ACCESS,
        secret_key=secret,
        prefix=prefix,
        transport=httpx.MockTransport(fake.handler),
    )


def test_put_get_exists_delete_and_clear() -> None:
    fake = FakeS3()
    s3 = store(fake)
    s3.ensure_bucket()
    s3.ensure_bucket()  # idempotent
    assert fake.buckets == {"artifacts"}
    ref = s3.put(b"report")
    assert ref.uri == f"sha256:{ref.sha256}" and ref.size_bytes == 6
    key = f"projects/alpha/sha256/{ref.sha256[:2]}/{ref.sha256}"
    assert set(fake.objects) == {key}
    puts = sum(r.method == "PUT" for r in fake.requests)
    s3.put(b"report")  # stored once
    assert sum(r.method == "PUT" for r in fake.requests) == puts
    assert s3.get(ref.sha256) == b"report" and s3.exists(ref.sha256)
    other = store(fake, prefix="projects/beta")
    assert not other.exists(ref.sha256)  # one prefix per project
    other.put(b"beta data")
    assert list(s3.keys()) == [key]
    fake.objects[key] = b"tampered"
    with pytest.raises(ArtifactIntegrityError):
        s3.get(ref.sha256)
    assert s3.delete(ref.sha256) is True and s3.delete(ref.sha256) is False
    s3.put(b"a")
    s3.put(b"b")
    assert s3.clear() == 2 and len(fake.objects) == 1  # beta's object stays
    with pytest.raises(FileNotFoundError):
        s3.get(hashlib.sha256(b"a").hexdigest())
    with pytest.raises(ValueError, match="digest"):
        s3.get("../../etc")
    s3.close()


def test_errors_and_configuration() -> None:
    fake = FakeS3()
    with pytest.raises(S3Error, match="SignatureDoesNotMatch"):
        store(fake, secret="wrong").put(b"x")
    with pytest.raises(ValueError, match="bucket"):
        S3ArtifactStore(endpoint="http://x", bucket="Bad_Bucket", access_key="a", secret_key="b")
    with pytest.raises(ValueError, match="http"):
        S3ArtifactStore(endpoint="ftp://x", bucket="ok-bucket", access_key="a", secret_key="b")
    with pytest.raises(ValueError, match="AI_CODING_S3_BUCKET"):
        S3ArtifactStore.from_env(prefix="p", env={"AI_CODING_S3_ENDPOINT": "http://x"})
    env = {
        "AI_CODING_S3_ENDPOINT": "https://s3.internal",
        "AI_CODING_S3_BUCKET": "art",
        "AI_CODING_S3_ACCESS_KEY_ID": "id",
        "AI_CODING_S3_SECRET_ACCESS_KEY": "secret",
    }
    s3 = S3ArtifactStore.from_env(prefix="projects/../x/./y", env=env)
    assert s3.prefix == "projects/x/y" and s3.region == "us-east-1"  # no path tricks
    assert re.fullmatch(r"s3://art/projects/x/y", s3.root)
    s3.close()


def test_factory_selects_the_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    _, config = render_initial_config(name="alpha")
    assert isinstance(make_artifact_store(config, tmp_path), LocalArtifactStore)
    minio = config.model_copy(
        update={"artifacts": config.artifacts.model_copy(update={"store": "minio"})}
    )
    with pytest.raises(ValueError, match="not set"):
        make_artifact_store(minio, tmp_path)
    for name, value in {
        "ENDPOINT": "http://m:9000",
        "BUCKET": "art",
        "ACCESS_KEY_ID": "a",
        "SECRET_ACCESS_KEY": "b",
    }.items():
        monkeypatch.setenv(f"AI_CODING_S3_{name}", value)
    s3 = make_artifact_store(minio, tmp_path)
    assert isinstance(s3, S3ArtifactStore) and s3.prefix == "projects/alpha"
    s3.close()
